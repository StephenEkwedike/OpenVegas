import hashlib
import json


def offer_digest(rows, max_tokens):
    offers = sorted((r['model'], min(r['max_tokens'], max_tokens), r['pricing']['fingerprint']) for r in rows)
    return hashlib.sha256(json.dumps(offers, separators=(',', ':')).encode()).hexdigest()
