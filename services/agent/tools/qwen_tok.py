"""Offline Qwen-family BPE token counter read straight from a GGUF's vocab + merges (no dependencies). Used as a PROXY for
Qwen2.5-7B-Instruct: Qwen3-VL's text tokenizer shares the Qwen2 byte-level BPE vocabulary. ASCII text only (approximates
\\p{L}/\\p{N} with [^\\W\\d_]/\\d), plus the chat-template special tokens counted as 1 each."""
import re
import struct
import sys

PATH = r"C:\Desktop\Qwen VLM\vlm_bench\models\Qwen3-VL-2B-Instruct-GGUF\Qwen3VL-2B-Instruct-Q4_K_M.gguf"
_FMT = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}


def load(path):
    f = open(path, "rb")
    assert f.read(4) == b"GGUF"
    ver, = struct.unpack("<I", f.read(4))
    nt, nkv = struct.unpack("<QQ", f.read(16))

    def rstr():
        n, = struct.unpack("<Q", f.read(8))
        return f.read(n).decode("utf-8", "replace")

    def rval(t):
        if t == 8:
            return rstr()
        if t == 9:
            et, n = struct.unpack("<IQ", f.read(12))
            if et == 8:
                return [rstr() for _ in range(n)]
            sz = struct.calcsize(_FMT[et])
            f.seek(sz * n, 1)
            return None
        return struct.unpack(_FMT[t], f.read(struct.calcsize(_FMT[t])))[0]

    kv = {}
    for _ in range(nkv):
        k = rstr()
        t, = struct.unpack("<I", f.read(4))
        v = rval(t)
        if k.startswith("tokenizer.ggml.") and k not in ("tokenizer.ggml.token_type", "tokenizer.ggml.scores"):
            kv[k] = v
        elif k in ("general.architecture",):
            kv[k] = v
    return kv


def bytes_to_unicode():
    bs = list(range(33, 127)) + list(range(161, 173)) + list(range(174, 256))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, map(chr, cs)))


class Tok:
    PAT = re.compile(r"(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\w]?[^\W\d_]+|\d| ?[^\s\w]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+")

    def __init__(self, path=PATH):
        kv = load(path)
        self.model, self.pre = kv.get("tokenizer.ggml.model"), kv.get("tokenizer.ggml.pre")
        self.vocab = {t: i for i, t in enumerate(kv["tokenizer.ggml.tokens"])}
        self.ranks = {tuple(m.split(" ", 1)): i for i, m in enumerate(kv["tokenizer.ggml.merges"])}
        self.b2u = bytes_to_unicode()
        self.special = re.compile(r"<\|[a-z_]+\|>")
        self.cache = {}

    def _bpe(self, w):
        if w in self.cache:
            return self.cache[w]
        parts = list(w)
        while len(parts) > 1:
            pairs = [(self.ranks.get((parts[i], parts[i + 1]), 1 << 60), i) for i in range(len(parts) - 1)]
            r, i = min(pairs)
            if r == 1 << 60:
                break
            parts[i:i + 2] = [parts[i] + parts[i + 1]]
        self.cache[w] = parts
        return parts

    def count(self, text):
        n = 0
        pos = 0
        for m in self.special.finditer(text):
            n += self._count_plain(text[pos:m.start()]) + 1
            pos = m.end()
        return n + self._count_plain(text[pos:])

    def _count_plain(self, text):
        n = 0
        for piece in self.PAT.findall(text):
            w = "".join(self.b2u[b] for b in piece.encode("utf-8"))
            n += len(self._bpe(w))
        return n


if __name__ == "__main__":
    t = Tok(sys.argv[1])
    print(t.model, t.pre, len(t.vocab), len(t.ranks), t.count("Hello, world! turn on the light."), file=sys.stderr)
