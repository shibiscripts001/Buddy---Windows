"""Images in the chat, for the page (page.py mixes this in): the picture
waiting in the composer, sending it up in parts, and fetching the images
of the messages on screen - one at a time, newest first, since a whole
page of them at once would bury the connection. Shrinking and checking
are images.py's; a DM image's encryption is e2e.py's.

Offered only where the server says it takes images (its welcome's
limits) and Pillow is there. Every image shown has been decoded and
checked here first (images.check), and a DM's decrypted; the page only
ever gets data: URLs.

To the view: attachment, image, images, lightbox. From it: attach_image,
paste_image, remove_attachment, save_image, show_image.
"""

from __future__ import annotations

import collections
import os

from PySide6.QtCore import QBuffer, QIODevice, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFileDialog

from core.i18n import tr, tr_filter

from . import e2e, images

CACHE_MAX = 80            # images kept ready to show (about 30 MB at most)
PICK_FILTER = "Pictures (*.png *.jpg *.jpeg *.webp *.gif *.bmp *.tif *.tiff);;All files (*)"
PICTURE_ENDINGS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")
EXTENSIONS = {"image/webp": ".webp", "image/jpeg": ".jpg", "image/png": ".png"}
CANT_SHOW = "This image can't be shown – it didn't arrive intact, or can't be opened on this PC."


class ImageMixin:
    # Uses from the page: client, me, rooms, messages, editing, device,
    # _readers, keystore, buddy_state, emit, _notify, _online, _server_url,
    # _in, _recipients.

    def _init_images(self):
        self.attachment = None            # {"data", "w", "h", "preview"} waiting in the composer
        self.image_limits = None          # the server's (welcome); None: it takes no images
        self._image_cache = collections.OrderedDict()   # id -> data URL, ready to show
        self._image_mime = {}             # id -> its type, for saving
        self._image_state = {}            # id -> "gone" or "broken": not asked for again
        self._image_meta = {}             # id -> {"room", "author", "enc"}: how to open it
        self._image_queue = []            # ids to fetch, most wanted first
        self._image_asked = None          # the one on its way
        self._image_open = None           # an id to show large as soon as it's here
        self._pending_images = {}         # nonce -> the attachment, until the server echoes the send
        self._upload_error = None

    def images_allowed(self) -> bool:
        return images.AVAILABLE and bool(self.image_limits) and self._online() and bool(
            self.me and self.me.get("name"))

    def _reset_images(self):
        """A new connection: whatever was on its way isn't any more."""
        self._image_asked, self._image_queue, self._upload_error = None, [], None
        self._pending_images = {}

    # ------------------------------------------------------------ composer

    def on_attach_image(self, _payload=None):
        if not self.images_allowed():
            return
        path, _chosen = QFileDialog.getOpenFileName(self, tr("Send a picture"), "", tr_filter(PICK_FILTER))
        if path:
            self._attach_file(path)

    def on_paste_image(self, _payload=None):
        """Ctrl+V with a picture (or a picture file copied in Explorer) on the clipboard."""
        if not self.images_allowed():
            return
        clipboard = QGuiApplication.clipboard()
        mime = clipboard.mimeData()
        files = [u.toLocalFile() for u in (mime.urls() if mime is not None and mime.hasUrls() else [])
                 if u.isLocalFile() and u.toLocalFile().lower().endswith(PICTURE_ENDINGS)]
        if files:
            self._attach_file(files[0])
            return
        image = clipboard.image()
        if image.isNull():
            return
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, "PNG")
        self._attach(bytes(buffer.data()))

    def on_files_dropped(self, paths):
        pictures = [p for p in paths if p.lower().endswith(PICTURE_ENDINGS)]
        if not self.images_allowed():
            return
        if not pictures:
            self._notify("Only pictures can be sent – PNG, JPEG, WebP, GIF, BMP or TIFF.")
            return
        self._attach_file(pictures[0])

    def _attach_file(self, path: str):
        try:
            if os.path.getsize(path) > images.MAX_INPUT_BYTES:
                raise images.ImageError(f"That file is over {images.MAX_INPUT_BYTES // (1024 * 1024)} MB – "
                                        "pick a smaller picture.")
            with open(path, "rb") as f:
                data = f.read()
        except images.ImageError as exc:
            self._notify(str(exc))
            return
        except OSError as exc:
            self._notify(f"Couldn't read that file: {exc.strerror or exc}")
            return
        self._attach(data)

    def _attach(self, data: bytes):
        if self.editing is not None:
            self._notify("Finish editing first – a picture goes with a new message.")
            return
        try:
            shrunk = images.shrink(data)
            preview = images.preview_url(shrunk.data)
        except images.ImageError as exc:
            self._notify(str(exc))
            return
        self.attachment = {"data": shrunk.data, "w": shrunk.w, "h": shrunk.h, "preview": preview}
        self._notify("")
        self._push_attachment(focus=True)

    def on_remove_attachment(self, _payload=None):
        self.attachment = None
        self._push_attachment(focus=True)

    def _push_attachment(self, focus=False):
        a = self.attachment
        days = (self.image_limits or {}).get("image_days", 7)
        self.emit("attachment", {
            "allowed": self.images_allowed(), "focus": focus,
            "preview": a["preview"] if a else None,
            "label": f"{a['w']} x {a['h']}, {images.size_label(len(a['data']))}" if a else "",
            "note": f"Images stay on the server for {days} days.",
        })

    # ------------------------------------------------------------- sending

    def _upload_image(self, room: dict, attachment: dict) -> dict | None:
        """Sends the picture up in parts (a DM's encrypted first) and returns
        what the send says about it; None if it can't go (said why)."""
        image_id = os.urandom(16).hex()
        data = attachment["data"]
        image = {"id": image_id, "w": attachment["w"], "h": attachment["h"]}
        if room["kind"] == "dm":
            recipients = self._recipients(room)
            if recipients is None:
                return None
            data, image["enc"] = e2e.encrypt_image(data, room=room["id"], sender=self.me["id"], image_id=image_id,
                                                   me=self.device, recipients=recipients)
        text = e2e.b64(data)
        size = max(1000, int(self.image_limits.get("image_part_chars") or 24000))
        pieces = [text[i:i + size] for i in range(0, len(text), size)]
        self._upload_error = None
        for seq, piece in enumerate(pieces):
            if not self.client.send({"type": "image_part", "id": image_id, "seq": seq, "data": piece,
                                     "last": seq == len(pieces) - 1}):
                return None
        # Your own picture shows straight away - it's already here.
        url = images.data_url(attachment["data"], "image/webp")
        self._remember_image(image_id, url, "image/webp")
        self.emit("image", {"id": image_id, "url": url})
        return image

    def _sent(self, nonce):
        """The server echoed a send: its picture has gone."""
        self._pending_images.pop(nonce, None)

    def _send_failed(self, msg: dict, text: str) -> str:
        """A send the server refused: its picture goes back in the composer
        (unless another is there now). Returns what to tell the user - why
        the upload failed, if that's the real reason."""
        attachment = self._pending_images.pop(msg.get("nonce"), None)
        if attachment is not None:
            if self.attachment is None:
                self.attachment = attachment
                self._push_attachment()
            if self._upload_error and msg.get("code") == "bad_image":
                text = self._upload_error
        self._upload_error = None
        return text

    # ------------------------------------------------------------ fetching

    def _showable(self, m: dict) -> bool:
        image = m.get("image")
        return (isinstance(image, dict) and isinstance(image.get("id"), str) and not image.get("gone")
                and not m.get("deleted") and not m.get("unreadable") and not m.get("replayed")
                and not self._in("blocked", m["author"].get("id", "")))

    def _want_images(self):
        """Asks for the images of the messages loaded here that aren't in
        hand yet, newest first (the bottom of the chat is what's on screen)."""
        wanted = []
        for m in sorted(self.messages.values(), key=lambda m: m["id"], reverse=True):
            if not self._showable(m):
                continue
            image_id = m["image"]["id"]
            if image_id in self._image_cache or image_id in self._image_state:
                continue
            self._image_meta[image_id] = {"room": m["room"], "author": m["author"].get("id") or "",
                                          "enc": m["image"].get("enc")}
            wanted.append(image_id)
        if self._image_open in self._image_meta and self._image_open not in wanted:
            wanted.insert(0, self._image_open)
        self._image_queue = wanted
        self._next_image()

    def _next_image(self):
        if self._image_asked is not None or not self._online():
            return
        while self._image_queue:
            image_id = self._image_queue.pop(0)
            if image_id in self._image_cache or image_id in self._image_state:
                continue
            self._image_asked = image_id
            self.client.send({"type": "get_image", "id": image_id})
            return

    def _remember_image(self, image_id: str, url: str, mime: str):
        self._image_cache[image_id] = url
        self._image_cache.move_to_end(image_id)
        self._image_mime[image_id] = mime
        while len(self._image_cache) > CACHE_MAX:
            old, _url = self._image_cache.popitem(last=False)
            self._image_mime.pop(old, None)

    def _image_arrived(self, msg: dict):
        image_id = msg.get("id")
        if image_id == self._image_asked:
            self._image_asked = None
        meta = self._image_meta.get(image_id)
        raw = e2e.unb64(msg.get("data"))
        mime = None
        if meta is not None and raw:
            if meta["enc"] is not None:
                raw = e2e.decrypt_image(raw, meta["enc"], room=meta["room"], sender=meta["author"], image_id=image_id,
                                        readers=self._readers,
                                        sender_keys=self.keystore.known_keys(self._server_url(), meta["author"]))
            mime = images.check(raw) if raw else None
        if meta is None:
            pass   # nothing asked for it
        elif mime:
            url = images.data_url(raw, mime)
            self._remember_image(image_id, url, mime)
            self.emit("image", {"id": image_id, "url": url})
            if self._image_open == image_id:
                self._image_open = None
                self.emit("lightbox", {"id": image_id, "url": url})
        else:
            self._image_state[image_id] = "broken"
            self.emit("image", {"id": image_id, "failed": CANT_SHOW})
        self._next_image()

    def _image_error(self, msg: dict) -> bool:
        """Handles an error about images; False if it wasn't one."""
        if msg.get("re") == "image_part":
            if self._upload_error is None:   # the first says why; the rest only follow from it
                self._upload_error = msg.get("message")
            return True
        if msg.get("re") != "get_image":
            return False
        image_id = msg.get("id")
        if image_id == self._image_asked:
            self._image_asked = None
        if msg.get("code") == "rate_limited" and isinstance(image_id, str):
            self._image_queue.insert(0, image_id)
            wait = msg.get("retry_after") if isinstance(msg.get("retry_after"), (int, float)) else 2
            QTimer.singleShot(int(max(0.5, min(wait, 30)) * 1000), self._next_image)
            return True
        if isinstance(image_id, str):
            self._image_state[image_id] = "gone"
            self.emit("image", {"id": image_id, "gone": True, "text": msg.get("message") or "This image has expired."})
            if self._image_open == image_id:
                self._image_open = None
                self._notify(msg.get("message") or "That image has expired.")
        self._next_image()
        return True

    def _push_images(self):
        """Everything already in hand, for a view that's just (re)loaded."""
        self.emit("images", {"ready": [{"id": i, "url": u} for i, u in self._image_cache.items()],
                             "failed": [{"id": i, "gone": s == "gone"} for i, s in self._image_state.items()],
                             "cant_show": CANT_SHOW})

    # ----------------------------------------------------- looking at one

    def show_image(self, image_id: str, room_id: str):
        """Shows an image large (a reported one, from the admin panel)."""
        if image_id in self._image_cache:
            self.emit("lightbox", {"id": image_id, "url": self._image_cache[image_id]})
            return
        if self._image_state.get(image_id) == "gone":
            self._notify("That image has expired.")
            return
        self._image_meta.setdefault(image_id, {"room": room_id, "author": "", "enc": None})
        self._image_open = image_id
        self._image_queue.insert(0, image_id)
        self._next_image()

    def on_save_image(self, payload):
        image_id = str((payload or {}).get("id") or "")
        url = self._image_cache.get(image_id)
        if not url:
            return
        extension = EXTENSIONS.get(self._image_mime.get(image_id, ""), ".webp")
        start = os.path.join(os.path.expanduser("~"), "Pictures", f"Buddy Network image{extension}")
        path, _chosen = QFileDialog.getSaveFileName(self, tr("Save image"), start, tr_filter(f"Image (*{extension})"))
        if not path:
            return
        try:
            with open(path, "wb") as f:
                f.write(e2e.unb64(url.split(",", 1)[1]) or b"")
        except OSError as exc:
            self._notify(f"Couldn't save the image: {exc.strerror or exc}")
            return
        self.emit("toast", {"text": "Image saved"})
