"""Small, conservative per-session memory for explicit forms of address."""

import re


# Each source spelling stays separate: changes in intimacy remain meaningful.
TERMS = {
    "ja": {
        "お兄さん": ("大哥哥", "小哥哥", "哥哥", "小哥", "兄长"),
        "お兄ちゃん": ("大哥哥", "小哥哥", "哥哥"),
        "ご主人様": ("主人", "少爷"),
        "先輩": ("前辈", "学长", "学姐"),
    },
    "en": {
        "dork": ("笨蛋", "呆子", "傻瓜", "书呆子"),
        "dummy": ("笨蛋", "傻瓜", "呆子"),
        "sweetheart": ("亲爱的", "宝贝", "甜心"),
    },
}


def address_terms(text, language):
    """Skip quotes, possessives and third-person references instead of guessing."""
    if re.search(r'[「」『』"“”]|\b(?:my|your|his|her|their|our|he|she|they|said|says|called)\b'
                 r'|(?:私|僕|俺|彼|彼女|友達|君|あなた)の|あの|その人|と呼|って呼', text, re.I):
        return []
    found = []
    for term in TERMS.get(language, {}):
        pattern = re.escape(term) if language == "ja" else r"\b" + re.escape(term) + r"\b"
        match = re.search(pattern, text, re.I)
        if match and language == "ja":
            before, after = text[:match.start()], text[match.end():]
            if not re.fullmatch(r"(?:\s|[、,!！?？。]|ねえ|ねぇ|かわいい|可愛い|おい)*", before):
                continue
            if after.strip() and not re.match(r"\s*[、。！？!?，,]", after):
                continue
        if match and language == "en":
            before, after = text[:match.start()], text[match.end():]
            if re.search(r"\b(?:a|an|the|this|that|some|another)\s+(?:\w+\s+){0,2}$", before, re.I):
                continue
            if after.strip() and not re.match(r"\s*[,!?.]", after):
                continue
        if match:
            found.append(term)
    # Multiple forms can refer to different people. No alignment inference here.
    return found if len(found) == 1 else []


class AddressMemory:
    def __init__(self):
        self.entries = {}

    def clear(self):
        self.entries.clear()

    def constraints(self, text, language):
        return {term: self.entries[(language, term)] for term in address_terms(text, language)
                if (language, term) in self.entries}

    def observe(self, text, translation, language):
        for term in address_terms(text, language):
            if (language, term) in self.entries:
                continue
            # Longest match first; exclude substrings inside another candidate.
            candidates = TERMS[language][term]
            matches = [word for word in candidates if word in translation]
            matches = [word for word in matches if not any(word != other and word in other for other in matches)]
            if len(matches) == 1:
                self.entries[(language, term)] = matches[0]

    def apply(self, text, translation, language):
        """Normalize one aligned address only; never invent a missing translation."""
        terms = address_terms(text, language)
        if not terms or re.search(r'[「」『』"“”]|(?:我|你|他|她|他们|她们)的|不是|别叫|不叫|称作|叫做', translation):
            return translation, None
        term = terms[0]
        matches = [word for word in TERMS[language][term] if word in translation]
        matches = [word for word in matches if not any(word != other and word in other for other in matches)]
        if len(matches) != 1 or translation.count(matches[0]) != 1:
            return translation, None
        word = matches[0]
        start = translation.index(word)
        before = re.split(r"[，、。！？!?：:]", translation[:start])[-1]
        after = translation[start + len(word):]
        if before not in ("", "可爱的", "亲爱的", "你这个", "小", "大"):
            return translation, None
        if after.strip() and not re.match(r"\s*[，、。！？!?：:,]", after):
            return translation, None
        key = (language, term)
        if key not in self.entries:
            self.entries[key] = word
            return translation, None
        chosen = self.entries[key]
        if chosen == word:
            return translation, None
        result = translation[:start] + chosen + translation[start + len(word):]
        return result, {"source": term, "from": word, "to": chosen, "original": translation}
