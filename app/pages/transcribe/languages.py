#!/usr/bin/env python3
"""
Language tables for translation. Pure data.

NLLB-200 names its languages with FLORES-200 codes (language + script,
"spa_Latn"); Whisper reports ISO 639-1 ("es"). NLLB_LANGUAGES is every code
in the model's own vocabulary - all 202, checked against
shared_vocabulary.json - with an English name. WHISPER_TO_NLLB maps what
Whisper can detect onto those; the few Whisper languages NLLB lacks
(Hawaiian, Latin, Breton) simply have no entry.

Where one language has several NLLB variants, the mapping picks the
written standard (Modern Standard Arabic, Simplified Chinese, Western
Persian, Bokmål) - what a transcript of "ar" / "zh" / "fa" / "no" speech
should be read as.
"""

from __future__ import annotations

NLLB_LANGUAGES = {
    "ace_Arab": "Acehnese (Arabic script)", "ace_Latn": "Acehnese (Latin script)",
    "acm_Arab": "Mesopotamian Arabic", "acq_Arab": "Ta'izzi-Adeni Arabic",
    "aeb_Arab": "Tunisian Arabic", "afr_Latn": "Afrikaans", "ajp_Arab": "South Levantine Arabic",
    "aka_Latn": "Akan", "amh_Ethi": "Amharic", "apc_Arab": "North Levantine Arabic",
    "arb_Arab": "Arabic (Modern Standard)", "ars_Arab": "Najdi Arabic",
    "ary_Arab": "Moroccan Arabic", "arz_Arab": "Egyptian Arabic", "asm_Beng": "Assamese",
    "ast_Latn": "Asturian", "awa_Deva": "Awadhi", "ayr_Latn": "Aymara (Central)",
    "azb_Arab": "Azerbaijani (South)", "azj_Latn": "Azerbaijani (North)", "bak_Cyrl": "Bashkir",
    "bam_Latn": "Bambara", "ban_Latn": "Balinese", "bel_Cyrl": "Belarusian", "bem_Latn": "Bemba",
    "ben_Beng": "Bengali", "bho_Deva": "Bhojpuri", "bjn_Arab": "Banjar (Arabic script)",
    "bjn_Latn": "Banjar (Latin script)", "bod_Tibt": "Tibetan", "bos_Latn": "Bosnian",
    "bug_Latn": "Buginese", "bul_Cyrl": "Bulgarian", "cat_Latn": "Catalan", "ceb_Latn": "Cebuano",
    "ces_Latn": "Czech", "cjk_Latn": "Chokwe", "ckb_Arab": "Kurdish (Sorani)",
    "crh_Latn": "Crimean Tatar", "cym_Latn": "Welsh", "dan_Latn": "Danish", "deu_Latn": "German",
    "dik_Latn": "Dinka (Southwestern)", "dyu_Latn": "Dyula", "dzo_Tibt": "Dzongkha",
    "ell_Grek": "Greek", "eng_Latn": "English", "epo_Latn": "Esperanto", "est_Latn": "Estonian",
    "eus_Latn": "Basque", "ewe_Latn": "Ewe", "fao_Latn": "Faroese", "pes_Arab": "Persian",
    "fij_Latn": "Fijian", "fin_Latn": "Finnish", "fon_Latn": "Fon", "fra_Latn": "French",
    "fur_Latn": "Friulian", "fuv_Latn": "Fulfulde (Nigerian)", "gla_Latn": "Scottish Gaelic",
    "gle_Latn": "Irish", "glg_Latn": "Galician", "grn_Latn": "Guarani", "guj_Gujr": "Gujarati",
    "hat_Latn": "Haitian Creole", "hau_Latn": "Hausa", "heb_Hebr": "Hebrew", "hin_Deva": "Hindi",
    "hne_Deva": "Chhattisgarhi", "hrv_Latn": "Croatian", "hun_Latn": "Hungarian",
    "hye_Armn": "Armenian", "ibo_Latn": "Igbo", "ilo_Latn": "Ilocano", "ind_Latn": "Indonesian",
    "isl_Latn": "Icelandic", "ita_Latn": "Italian", "jav_Latn": "Javanese", "jpn_Jpan": "Japanese",
    "kab_Latn": "Kabyle", "kac_Latn": "Jingpho", "kam_Latn": "Kamba", "kan_Knda": "Kannada",
    "kas_Arab": "Kashmiri (Arabic script)", "kas_Deva": "Kashmiri (Devanagari)",
    "kat_Geor": "Georgian", "knc_Arab": "Kanuri (Arabic script)",
    "knc_Latn": "Kanuri (Latin script)", "kaz_Cyrl": "Kazakh", "kbp_Latn": "Kabiyè",
    "kea_Latn": "Kabuverdianu", "khm_Khmr": "Khmer", "kik_Latn": "Kikuyu",
    "kin_Latn": "Kinyarwanda", "kir_Cyrl": "Kyrgyz", "kmb_Latn": "Kimbundu", "kon_Latn": "Kikongo",
    "kor_Hang": "Korean", "kmr_Latn": "Kurdish (Kurmanji)", "lao_Laoo": "Lao",
    "lvs_Latn": "Latvian", "lij_Latn": "Ligurian", "lim_Latn": "Limburgish", "lin_Latn": "Lingala",
    "lit_Latn": "Lithuanian", "lmo_Latn": "Lombard", "ltg_Latn": "Latgalian",
    "ltz_Latn": "Luxembourgish", "lua_Latn": "Luba-Kasai", "lug_Latn": "Ganda", "luo_Latn": "Luo",
    "lus_Latn": "Mizo", "mag_Deva": "Magahi", "mai_Deva": "Maithili", "mal_Mlym": "Malayalam",
    "mar_Deva": "Marathi", "min_Latn": "Minangkabau", "mkd_Cyrl": "Macedonian",
    "plt_Latn": "Malagasy", "mlt_Latn": "Maltese", "mni_Beng": "Meitei (Bengali script)",
    "khk_Cyrl": "Mongolian", "mos_Latn": "Mossi", "mri_Latn": "Maori", "zsm_Latn": "Malay",
    "mya_Mymr": "Burmese", "nld_Latn": "Dutch", "nno_Latn": "Norwegian Nynorsk",
    "nob_Latn": "Norwegian Bokmål", "npi_Deva": "Nepali", "nso_Latn": "Northern Sotho",
    "nus_Latn": "Nuer", "nya_Latn": "Nyanja", "oci_Latn": "Occitan", "gaz_Latn": "Oromo",
    "ory_Orya": "Odia", "pag_Latn": "Pangasinan", "pan_Guru": "Punjabi", "pap_Latn": "Papiamento",
    "pol_Latn": "Polish", "por_Latn": "Portuguese", "prs_Arab": "Dari", "pbt_Arab": "Pashto",
    "quy_Latn": "Quechua (Ayacucho)", "ron_Latn": "Romanian", "run_Latn": "Kirundi",
    "rus_Cyrl": "Russian", "sag_Latn": "Sango", "san_Deva": "Sanskrit", "sat_Beng": "Santali",
    "scn_Latn": "Sicilian", "shn_Mymr": "Shan", "sin_Sinh": "Sinhala", "slk_Latn": "Slovak",
    "slv_Latn": "Slovenian", "smo_Latn": "Samoan", "sna_Latn": "Shona", "snd_Arab": "Sindhi",
    "som_Latn": "Somali", "sot_Latn": "Southern Sotho", "spa_Latn": "Spanish",
    "als_Latn": "Albanian", "srd_Latn": "Sardinian", "srp_Cyrl": "Serbian", "ssw_Latn": "Swati",
    "sun_Latn": "Sundanese", "swe_Latn": "Swedish", "swh_Latn": "Swahili", "szl_Latn": "Silesian",
    "tam_Taml": "Tamil", "tat_Cyrl": "Tatar", "tel_Telu": "Telugu", "tgk_Cyrl": "Tajik",
    "tgl_Latn": "Tagalog", "tha_Thai": "Thai", "tir_Ethi": "Tigrinya",
    "taq_Latn": "Tamasheq (Latin script)", "taq_Tfng": "Tamasheq (Tifinagh)",
    "tpi_Latn": "Tok Pisin", "tsn_Latn": "Tswana", "tso_Latn": "Tsonga", "tuk_Latn": "Turkmen",
    "tum_Latn": "Tumbuka", "tur_Latn": "Turkish", "twi_Latn": "Twi",
    "tzm_Tfng": "Tamazight (Central Atlas)", "uig_Arab": "Uyghur", "ukr_Cyrl": "Ukrainian",
    "umb_Latn": "Umbundu", "urd_Arab": "Urdu", "uzn_Latn": "Uzbek", "vec_Latn": "Venetian",
    "vie_Latn": "Vietnamese", "war_Latn": "Waray", "wol_Latn": "Wolof", "xho_Latn": "Xhosa",
    "ydd_Hebr": "Yiddish", "yor_Latn": "Yoruba", "yue_Hant": "Cantonese",
    "zho_Hans": "Chinese (Simplified)", "zho_Hant": "Chinese (Traditional)", "zul_Latn": "Zulu",
}

WHISPER_TO_NLLB = {
    "af": "afr_Latn", "am": "amh_Ethi", "ar": "arb_Arab", "as": "asm_Beng", "az": "azj_Latn",
    "ba": "bak_Cyrl", "be": "bel_Cyrl", "bg": "bul_Cyrl", "bn": "ben_Beng", "bo": "bod_Tibt",
    "bs": "bos_Latn", "ca": "cat_Latn", "cs": "ces_Latn", "cy": "cym_Latn", "da": "dan_Latn",
    "de": "deu_Latn", "el": "ell_Grek", "en": "eng_Latn", "es": "spa_Latn", "et": "est_Latn",
    "eu": "eus_Latn", "fa": "pes_Arab", "fi": "fin_Latn", "fo": "fao_Latn", "fr": "fra_Latn",
    "gl": "glg_Latn", "gu": "guj_Gujr", "ha": "hau_Latn", "he": "heb_Hebr", "hi": "hin_Deva",
    "hr": "hrv_Latn", "ht": "hat_Latn", "hu": "hun_Latn", "hy": "hye_Armn", "id": "ind_Latn",
    "is": "isl_Latn", "it": "ita_Latn", "ja": "jpn_Jpan", "jw": "jav_Latn", "jv": "jav_Latn",
    "ka": "kat_Geor", "kk": "kaz_Cyrl", "km": "khm_Khmr", "kn": "kan_Knda", "ko": "kor_Hang",
    "lb": "ltz_Latn", "ln": "lin_Latn", "lo": "lao_Laoo", "lt": "lit_Latn", "lv": "lvs_Latn",
    "mg": "plt_Latn", "mi": "mri_Latn", "mk": "mkd_Cyrl", "ml": "mal_Mlym", "mn": "khk_Cyrl",
    "mr": "mar_Deva", "ms": "zsm_Latn", "mt": "mlt_Latn", "my": "mya_Mymr", "ne": "npi_Deva",
    "nl": "nld_Latn", "nn": "nno_Latn", "no": "nob_Latn", "oc": "oci_Latn", "pa": "pan_Guru",
    "pl": "pol_Latn", "ps": "pbt_Arab", "pt": "por_Latn", "ro": "ron_Latn", "ru": "rus_Cyrl",
    "sa": "san_Deva", "sd": "snd_Arab", "si": "sin_Sinh", "sk": "slk_Latn", "sl": "slv_Latn",
    "sn": "sna_Latn", "so": "som_Latn", "sq": "als_Latn", "sr": "srp_Cyrl", "su": "sun_Latn",
    "sv": "swe_Latn", "sw": "swh_Latn", "ta": "tam_Taml", "te": "tel_Telu", "tg": "tgk_Cyrl",
    "th": "tha_Thai", "tk": "tuk_Latn", "tl": "tgl_Latn", "tr": "tur_Latn", "tt": "tat_Cyrl",
    "uk": "ukr_Cyrl", "ur": "urd_Arab", "uz": "uzn_Latn", "vi": "vie_Latn", "yi": "ydd_Hebr",
    "yo": "yor_Latn", "yue": "yue_Hant", "zh": "zho_Hans",
}

# MADLAD-400 (Google) is steered by a "<2xx>" tag at the start of its input.
# NLLB code -> that tag, for every NLLB language MADLAD also has: built from
# the tags in the model's own shared_vocabulary.json (Nextcloud-AI's
# madlad400-3b-mt-ct2-int8), preferring an exact script-qualified tag
# ("ace_Arab"), then the ISO 639-1 code ("de"), then 639-3 ("bho"). MADLAD
# has no Cantonese and few of NLLB's Arabic dialects and African languages;
# those have no entry and are skipped.
MADLAD_CODES = {
    "ace_Arab": "ace_Arab", "ace_Latn": "ace", "afr_Latn": "af", "aka_Latn": "ak",
    "als_Latn": "sq", "amh_Ethi": "am", "arb_Arab": "ar", "ary_Arab": "ary", "arz_Arab": "arz",
    "asm_Beng": "as", "awa_Deva": "awa", "ayr_Latn": "ay", "azj_Latn": "az", "bak_Cyrl": "ba",
    "bam_Latn": "bm", "ban_Latn": "ban", "bel_Cyrl": "be", "ben_Beng": "bn", "bho_Deva": "bho",
    "bjn_Arab": "bjn_Arab", "bjn_Latn": "bjn", "bod_Tibt": "bo", "bos_Latn": "bs",
    "bug_Latn": "bug", "bul_Cyrl": "bg", "cat_Latn": "ca", "ceb_Latn": "ceb", "ces_Latn": "cs",
    "ckb_Arab": "ckb", "crh_Latn": "crh_Latn", "cym_Latn": "cy", "dan_Latn": "da",
    "deu_Latn": "de", "dyu_Latn": "dyu", "dzo_Tibt": "dz", "ell_Grek": "el", "eng_Latn": "en",
    "epo_Latn": "eo", "est_Latn": "et", "eus_Latn": "eu", "ewe_Latn": "ee", "fao_Latn": "fo",
    "fij_Latn": "fj", "fin_Latn": "fi", "fon_Latn": "fon", "fra_Latn": "fr", "fur_Latn": "fur",
    "fuv_Latn": "fuv", "gaz_Latn": "om", "gla_Latn": "gd", "gle_Latn": "ga", "glg_Latn": "gl",
    "grn_Latn": "gn", "guj_Gujr": "gu", "hat_Latn": "ht", "hau_Latn": "ha", "heb_Hebr": "he",
    "hin_Deva": "hi", "hne_Deva": "hne", "hrv_Latn": "hr", "hun_Latn": "hu", "hye_Armn": "hy",
    "ibo_Latn": "ig", "ilo_Latn": "ilo", "ind_Latn": "id", "isl_Latn": "is", "ita_Latn": "it",
    "jav_Latn": "jv", "jpn_Jpan": "ja", "kac_Latn": "kac", "kan_Knda": "kn", "kas_Arab": "ks",
    "kas_Deva": "ks_Deva", "kat_Geor": "ka", "kaz_Cyrl": "kk", "kbp_Latn": "kbp", "khk_Cyrl": "mn",
    "khm_Khmr": "km", "kin_Latn": "rw", "kir_Cyrl": "ky", "kmb_Latn": "kmb", "kmr_Latn": "ku",
    "kon_Latn": "kg", "kor_Hang": "ko", "lao_Laoo": "lo", "lij_Latn": "lij", "lim_Latn": "li",
    "lin_Latn": "ln", "lit_Latn": "lt", "lmo_Latn": "lmo", "ltg_Latn": "ltg", "ltz_Latn": "lb",
    "lug_Latn": "lg", "lus_Latn": "lus", "lvs_Latn": "lv", "mag_Deva": "mag", "mai_Deva": "mai",
    "mal_Mlym": "ml", "mar_Deva": "mr", "min_Latn": "min", "mkd_Cyrl": "mk", "mlt_Latn": "mt",
    "mni_Beng": "mni", "mri_Latn": "mi", "mya_Mymr": "my", "nld_Latn": "nl", "nno_Latn": "nn",
    "nob_Latn": "no", "npi_Deva": "ne", "nso_Latn": "nso", "nus_Latn": "nus", "nya_Latn": "ny",
    "oci_Latn": "oc", "ory_Orya": "or", "pag_Latn": "pag", "pan_Guru": "pa", "pap_Latn": "pap",
    "pbt_Arab": "ps", "pes_Arab": "fa", "plt_Latn": "mg", "pol_Latn": "pl", "por_Latn": "pt",
    "prs_Arab": "prs", "quy_Latn": "qu", "ron_Latn": "ro", "run_Latn": "rn", "rus_Cyrl": "ru",
    "sag_Latn": "sg", "san_Deva": "sa", "scn_Latn": "scn", "shn_Mymr": "shn", "sin_Sinh": "si",
    "slk_Latn": "sk", "slv_Latn": "sl", "smo_Latn": "sm", "sna_Latn": "sn", "snd_Arab": "sd",
    "som_Latn": "so", "sot_Latn": "st", "spa_Latn": "es", "srd_Latn": "sc", "srp_Cyrl": "sr",
    "ssw_Latn": "ss", "sun_Latn": "su", "swe_Latn": "sv", "swh_Latn": "sw", "szl_Latn": "szl",
    "tam_Taml": "ta", "taq_Latn": "taq", "taq_Tfng": "taq_Tfng", "tat_Cyrl": "tt",
    "tel_Telu": "te", "tgk_Cyrl": "tg", "tgl_Latn": "fil", "tha_Thai": "th", "tir_Ethi": "ti",
    "tsn_Latn": "tn", "tso_Latn": "ts", "tuk_Latn": "tk", "tur_Latn": "tr", "tzm_Tfng": "tzm",
    "uig_Arab": "ug", "ukr_Cyrl": "uk", "urd_Arab": "ur", "uzn_Latn": "uz", "vec_Latn": "vec",
    "vie_Latn": "vi", "war_Latn": "war", "wol_Latn": "wo", "xho_Latn": "xh", "ydd_Hebr": "yi",
    "yor_Latn": "yo", "zho_Hans": "zh", "zho_Hant": "zh_Hant", "zsm_Latn": "ms", "zul_Latn": "zu",
}

# Full-width scripts: subtitle lines are shorter (style guides give 13-16
# per line for Japanese and Chinese, against 42 for Latin text) - see
# subtitles.CJK_WIDTH.
CJK_CODES = {"jpn_Jpan", "zho_Hans", "zho_Hant", "yue_Hant"}
# Scripts written without spaces between words: word pieces are joined
# as-is (not with a space each) and lines may wrap between characters.
# Thai, Lao, Burmese and Khmer are no-space but not full-width, so they
# keep the normal line length.
NO_SPACE_CODES = CJK_CODES | {"tha_Thai", "lao_Laoo", "mya_Mymr", "khm_Khmr"}


def sorted_targets() -> list[tuple[str, str]]:
    """(name, code) for every NLLB language, alphabetical by name."""
    return sorted(((name, code) for code, name in NLLB_LANGUAGES.items()),
                  key=lambda x: x[0].lower())


def name_of(code: str) -> str:
    return NLLB_LANGUAGES.get(code, code)
