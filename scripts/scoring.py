"""原Hotpot归一化与F1，保持历史评分语义，供执行器和公共门禁共同使用。"""

import re
import string
from collections import Counter


def normalize(text):
    text = str(text).lower()
    text = "".join(ch for ch in text if ch not in string.punctuation)
    return " ".join(re.sub(r"\b(a|an|the)\b", " ", text).split())


def f1_score(pred, gold):
    pred, gold = normalize(pred), normalize(gold)
    if pred != gold and (pred in {"yes", "no", "noanswer"} or gold in {"yes", "no", "noanswer"}):
        return 0.0
    p, g = pred.split(), gold.split()
    common = sum((Counter(p) & Counter(g)).values())
    return 2 * common / (len(p) + len(g)) if common else 0.0
