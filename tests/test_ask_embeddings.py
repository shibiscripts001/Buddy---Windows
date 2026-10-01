"""Ask Buddy's semantic search without Ollama: Buddy's own llama.cpp
(local_llama.py), the embedders (embedder.py) and the retriever's check that
an embedder's vectors are the bundle's (retrieval.py). No network, no Qt,
nothing run: downloads, the scan and the server are stood in for."""

import hashlib
import io
import json
import os
import struct
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from pages.manual_chat import embedder as emb  # noqa: E402
from pages.manual_chat import local_llama as ll  # noqa: E402
from pages.manual_chat import retrieval  # noqa: E402
from pages.manual_chat.bundle_builder import quantize  # noqa: E402


class _Response(io.BytesIO):
    """urlopen's answer: the bytes, as a context manager with headers."""

    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _TempRoot(unittest.TestCase):
    """local_llama's folders under a temp dir for each test."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        names = {"ROOT": self.root, "RUNTIME_DIR": self.root / "llama.cpp" / ll.RELEASE,
                 "MODELS_DIR": self.root / "models", "LOG_PATH": self.root / "llama.cpp" / "server.log"}
        for name, value in names.items():
            patcher = mock.patch.object(ll, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def item(self, data, name="thing.zip"):
        return ll.Download("https://example.invalid/" + name, name, len(data), hashlib.sha256(data).hexdigest(), "Thing")

    def serve(self, data):
        return mock.patch.object(ll.urllib.request, "urlopen", lambda *_a, **_k: _Response(data))


class DownloadTests(_TempRoot):
    def test_a_download_that_does_not_match_its_checksum_is_refused_and_nothing_is_kept(self):
        item = self.item(b"the real thing")
        dest = self.root / "thing.zip"
        with self.serve(b"something else"), self.assertRaises(ll.LlamaError) as caught:
            ll.download(item, dest)
        self.assertIn("checksum", str(caught.exception))
        self.assertEqual(list(self.root.iterdir()), [])                 # no file, no .part

    def test_a_matching_download_lands_whole(self):
        item = self.item(b"the real thing")
        with self.serve(b"the real thing"):
            path = ll.download(item, self.root / "thing.zip")
        self.assertEqual(path.read_bytes(), b"the real thing")

    def test_the_pinned_files_are_checked_against_sha256(self):
        for item in [*ll.RUNTIMES.values(), ll.EMBEDDING_GEMMA]:
            self.assertRegex(item.sha256, r"^[0-9a-f]{64}$")
            self.assertTrue(item.url.startswith("https://"))
        self.assertIn(f"/download/{ll.RELEASE}/", ll.RUNTIMES[("Windows", "AMD64")].url)    # one pinned release


class InstallTests(_TempRoot):
    def runtime_zip(self, extra=()):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("llama-server.exe" if os.name == "nt" else "llama-server", b"MZ")
            for name, data in extra:
                z.writestr(name, data)
        return buf.getvalue()

    def install(self, data, verdict=(ll.SCAN_CLEAN, "scanned")):
        item = self.item(data, "llama.zip")
        with mock.patch.object(ll, "runtime_download", lambda: item), self.serve(data), \
                mock.patch.object(ll, "defender_scan", lambda _folder: verdict):
            return ll.install_runtime()

    def test_it_is_unpacked_scanned_and_the_archive_removed(self):
        exe, note = self.install(self.runtime_zip())
        self.assertTrue(exe.is_file())
        self.assertEqual(note, "scanned")
        self.assertFalse((ll.RUNTIME_DIR.parent / "llama.zip").exists())

    def test_a_runtime_defender_flags_is_deleted_and_never_used(self):
        with self.assertRaises(ll.LlamaError) as caught:
            self.install(self.runtime_zip(), verdict=(ll.SCAN_THREAT, ""))
        self.assertIn("Defender", str(caught.exception))
        self.assertIsNone(ll.server_exe())
        self.assertFalse(any(ll.RUNTIME_DIR.parent.glob("*.unpacking")))

    def test_nothing_in_an_archive_lands_outside_its_folder(self):
        self.install(self.runtime_zip(extra=[("../../escaped.txt", b"x")]))
        self.assertFalse((self.root / "escaped.txt").exists())
        self.assertFalse((self.root.parent / "escaped.txt").exists())

    def test_a_tar_with_an_escaping_path_stays_inside_too(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for name in ("llama/llama-server", "../escaped.txt"):
                info = tarfile.TarInfo(name)
                info.size = 2
                t.addfile(info, io.BytesIO(b"ok"))
        data = buf.getvalue()
        item = self.item(data, "llama.tar.gz")
        with mock.patch.object(ll, "runtime_download", lambda: item), self.serve(data), \
                mock.patch.object(ll, "defender_scan", lambda _folder: (ll.SCAN_SKIPPED, "")), \
                mock.patch.object(ll.os, "name", "posix"):
            try:
                ll.install_runtime()
            except ll.LlamaError:
                pass        # refused outright is fine too
        self.assertFalse((self.root / "llama.cpp" / "escaped.txt").exists())
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_defender_reads_threats_from_its_exit_code(self):
        from core import defender
        with mock.patch.object(defender.os, "name", "nt"), mock.patch.dict(os.environ, {"ProgramFiles": str(self.root)}), \
                mock.patch.object(defender.Path, "is_file", lambda _p: True):
            for code, verdict in ((0, ll.SCAN_CLEAN), (2, ll.SCAN_THREAT), (5, ll.SCAN_SKIPPED)):
                with mock.patch.object(defender.subprocess, "run", return_value=mock.Mock(returncode=code)) as run:
                    self.assertEqual(ll.defender_scan(self.root)[0], verdict)
                args = run.call_args[0][0]
                self.assertIn("-DisableRemediation", args)
                self.assertIn(str(self.root), args)


class ModelFileTests(_TempRoot):
    def test_only_real_gguf_files_are_offered(self):
        ll.ensure_models_dir()
        (ll.MODELS_DIR / "embeddinggemma-300M-Q8_0.gguf").write_bytes(b"GGUF....")
        (ll.MODELS_DIR / "renamed-pickle.gguf").write_bytes(b"\x80\x04 a pytorch pickle")
        (ll.MODELS_DIR / "model.bin").write_bytes(b"GGUF but the wrong name")
        (ll.MODELS_DIR / "mmproj-vision.gguf").write_bytes(b"GGUF")
        (ll.MODELS_DIR / "big-00001-of-00002.gguf").write_bytes(b"GGUF")
        (ll.MODELS_DIR / "big-00002-of-00002.gguf").write_bytes(b"GGUF")
        self.assertEqual([p.name for p in ll.model_files()], ["big-00001-of-00002.gguf", "embeddinggemma-300M-Q8_0.gguf"])

    def test_a_chat_model_is_never_guessed_to_be_the_embedding_one(self):
        ll.ensure_models_dir()
        (ll.MODELS_DIR / "qwen3-8b-Q4_K_M.gguf").write_bytes(b"GGUF")
        self.assertIsNone(emb.chosen_model_file({}))
        (ll.MODELS_DIR / "nomic-embed-text.gguf").write_bytes(b"GGUF")
        self.assertEqual(emb.chosen_model_file({}).name, "nomic-embed-text.gguf")
        self.assertEqual(emb.chosen_model_file({"embed_model_file": "qwen3-8b-Q4_K_M.gguf"}).name,
                         "qwen3-8b-Q4_K_M.gguf")                         # picked by hand: theirs to pick


class ServerTests(_TempRoot):
    def test_it_listens_on_this_computer_only_with_a_key_and_no_network(self):
        ll.ensure_models_dir()
        model = ll.MODELS_DIR / "embeddinggemma.gguf"
        model.write_bytes(b"GGUF")
        exe = self.root / "llama-server.exe"
        exe.write_bytes(b"")
        server = ll.LlamaServer(model)
        with mock.patch.object(ll, "server_exe", lambda: exe), \
                mock.patch.object(ll.subprocess, "Popen") as popen, \
                mock.patch.object(ll.LlamaServer, "_wait_ready", lambda *_a: None), \
                mock.patch.object(ll, "_JOB", mock.Mock()):
            popen.return_value.poll.return_value = None
            popen.return_value.pid = 1
            base = server.ensure()
        args = popen.call_args[0][0]
        self.assertEqual(args[args.index("--host") + 1], "127.0.0.1")
        self.assertEqual(args[args.index("--api-key") + 1], server.key)
        self.assertGreaterEqual(len(server.key), 24)
        self.assertIn("--offline", args)
        self.assertIn("--no-webui", args)
        self.assertIn("--embedding", args)
        self.assertTrue(base.startswith("http://127.0.0.1:"))
        server.process = None

    def test_a_file_that_is_not_gguf_is_never_run(self):
        ll.ensure_models_dir()
        model = ll.MODELS_DIR / "fake.gguf"
        model.write_bytes(b"#!/bin/sh")
        with mock.patch.object(ll, "server_exe", lambda: self.root / "llama-server"), \
                mock.patch.object(ll.subprocess, "Popen") as popen, self.assertRaises(ll.LlamaError):
            ll.LlamaServer(model).ensure()
        popen.assert_not_called()


class _Fake(emb.Embedder):
    """An embedder whose vectors are given; ready unless told otherwise."""

    label = "Fake"

    def __init__(self, vector_for, ready=(True, "")):
        self.vector_for, self._ready = vector_for, ready
        self.calls = 0

    def ready(self):
        return self._ready

    def embed(self, texts, timeout=0):
        self.calls += 1
        return [self.vector_for(t) for t in texts]


def _vec(seed, dim=512):
    import random
    rng = random.Random(seed)
    return [rng.uniform(-1, 1) for _ in range(dim)]


class RetrieverTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        chunks = [{"text": "Noise reduction removes hum from dialogue", "page": 10, "chapter_title": "Fairlight"},
                  {"text": "Keyframes animate parameters over time", "page": 20, "chapter_title": "Fusion"},
                  {"text": "Export timelines as XML or EDL", "page": 30, "chapter_title": "Edit"}]
        (self.dir / retrieval.BUNDLE_CHUNKS).write_text("\n".join(json.dumps(c) for c in chunks), encoding="utf-8")
        self.doc_vecs = [_vec(i) for i in range(3)]
        rows = [quantize(v) for v in self.doc_vecs]
        (self.dir / retrieval.BUNDLE_VECTORS).write_bytes(
            retrieval.VECTOR_MAGIC + struct.pack("<ii", 3, 512) + struct.pack("<3f", *[s for s, _ in rows])
            + b"".join(q for _, q in rows))
        self.first_doc = retrieval.DOC_PREFIX.format(title="Fairlight") + chunks[0]["text"]

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def matching(self):
        # The bundle's model: chunk 0 embeds to its stored vector; queries
        # land on the chunk they paraphrase.
        return lambda t: self.doc_vecs[0] if t == self.first_doc else self.doc_vecs[2] if "XML" in t or "xml" in t \
            else self.doc_vecs[1]

    def test_the_bundles_own_model_searches_semantically(self):
        fake = _Fake(self.matching())
        r = retrieval.ManualRetriever(self.dir, None, fake)
        self.assertEqual(r.tier, retrieval.TIER_HYBRID)
        self.assertEqual(r.search("save the cut for another app as xml", 3)[0].page, 30)
        r.search("again", 3)
        self.assertEqual(fake.calls, 2)                                  # checked once, not per question
        self.assertIn("Fake", r.describe_tier())

    def test_another_model_falls_back_to_keywords_and_says_why(self):
        r = retrieval.ManualRetriever(self.dir, None, _Fake(lambda t: _vec(hash(t) % 1000)))
        self.assertEqual(r.search("hum", 3)[0].page, 10)                 # still answers, on keywords
        self.assertEqual(r.tier, retrieval.TIER_BUNDLE_BM25)
        self.assertIn("isn't the model", r.describe_tier())

    def test_an_embedder_that_is_not_ready_is_keyword_search_with_its_reason(self):
        r = retrieval.ManualRetriever(self.dir, None, _Fake(self.matching(), ready=(False, "Ollama isn't running.")))
        self.assertEqual(r.tier, retrieval.TIER_BUNDLE_BM25)
        self.assertIn("Ollama isn't running.", r.describe_tier())
        off = retrieval.ManualRetriever(self.dir, None, None)
        self.assertIn("off in Settings", off.describe_tier())

    def test_an_embedder_that_stops_answering_mid_session_drops_to_keywords(self):
        fake = _Fake(self.matching())
        r = retrieval.ManualRetriever(self.dir, None, fake)
        r.search("xml", 3)
        fake.embed = mock.Mock(side_effect=emb.EmbedError("gone"))
        self.assertEqual(r.search("hum", 3)[0].page, 10)
        self.assertEqual(r.tier, retrieval.TIER_BUNDLE_BM25)

    def test_settings_test_checks_the_vectors_not_just_an_answer(self):
        ok, message = retrieval.check_embedder(self.dir, _Fake(self.matching()))
        self.assertTrue(ok, message)
        ok, message = retrieval.check_embedder(self.dir, _Fake(lambda t: _vec(7)))
        self.assertFalse(ok)
        self.assertIn("isn't the model", message)


class ChoiceTests(_TempRoot):
    def test_automatic_is_ollama_until_buddys_own_is_set_up(self):
        self.assertIsInstance(emb.from_settings({}), emb.OllamaEmbedder)
        ll.ensure_models_dir()
        (ll.MODELS_DIR / "embeddinggemma-300M-Q8_0.gguf").write_bytes(b"GGUF")
        exe = self.root / "llama-server.exe"
        with mock.patch.object(ll, "server_exe", lambda: exe):
            chosen = emb.from_settings({"embed_backend": "auto"})
        self.assertIsInstance(chosen, emb.BuddyEmbedder)
        self.assertEqual(chosen.model_name, "embeddinggemma")            # what the bundle's meta records

    def test_each_choice(self):
        self.assertIsNone(emb.from_settings({"embed_backend": "off"}))
        self.assertIsInstance(emb.from_settings({"embed_backend": "ollama"}), emb.OllamaEmbedder)
        server = emb.from_settings({"embed_backend": "server", "embed_base_url": "127.0.0.1:1234"})
        self.assertEqual(server.base_url, "http://127.0.0.1:1234/v1")
        self.assertFalse(server.leaves_machine)
        self.assertTrue(emb.ServerEmbedder("https://api.example.com/v1").leaves_machine)
        buddy = emb.from_settings({"embed_backend": "buddy"})
        self.assertIsInstance(buddy, emb.BuddyEmbedder)
        self.assertFalse(buddy.ready()[0])                               # not set up: says so, runs nothing


if __name__ == "__main__":
    unittest.main()
