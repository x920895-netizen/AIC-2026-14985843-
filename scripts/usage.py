"""原客户端的UTF-8字节/最大输出费用预留公式，无模型或网络依赖。"""

import json


def estimate_reservation(messages, max_tokens, prices):
    input_bound = len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 128 * len(messages) + 256
    return (input_bound * prices[0] + max_tokens * prices[1]) / 1e6
