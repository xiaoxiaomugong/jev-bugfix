#!/usr/bin/env python3
"""Offline Jev CLI double; behavior is selected through FAKE_JEV_MODE.

It deliberately does no network or shell work. FAKE_JEV_LOG records one JSON
object per invocation, including argv and the exact submitted JSONL payload.
"""

import copy
import json
import os
import sys
import time


UNTRUSTED_ERROR = "REMOTE_RAW_ERROR_API_KEY=do-not-print-this-secret"


def success(state, score):
    probabilities = {str(i): 0.0 for i in range(5)}
    probabilities[str(score)] = 1.0
    return {
        "input": state,
        "answer": {
            "type": "score",
            "score": score,
            "value": score,
            "label": str(score),
            "legend": {str(i): str(i) for i in range(5)},
            "probabilities": probabilities,
            "confidence": 1.0,
        },
    }


def emit(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=True), flush=True)


def main():
    raw = sys.stdin.buffer.read()
    lines = raw.decode("utf-8").splitlines()
    states = [json.loads(line) for line in lines if line]
    log_path = os.environ.get("FAKE_JEV_LOG")
    if log_path:
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(json.dumps({
                "argv": sys.argv[1:],
                "stdin": raw.decode("utf-8"),
                "payload_bytes": len(raw),
                "states": states,
            }, ensure_ascii=False) + "\n")

    if sys.argv[1:] == ["--version"]:
        version_mode = os.environ.get("FAKE_JEV_VERSION_MODE", "success")
        if version_mode == "timeout":
            time.sleep(5)
        elif version_mode in {"output_limit", "stderr_limit"}:
            stream = sys.stderr if version_mode == "stderr_limit" else sys.stdout
            print(UNTRUSTED_ERROR + "x" * 1500, file=stream, flush=True)
            return 0
        elif version_mode == "unknown":
            print("jev 0.4.0")
            return 0
        elif version_mode == "malformed":
            print(UNTRUSTED_ERROR + " jev 0.3.2")
            return 0
        elif version_mode == "empty":
            return 0
        print("jev 0.3.2")
        return 2 if version_mode == "nonzero" else 0

    mode = os.environ.get("FAKE_JEV_MODE", "success")
    if mode == "fatal":
        print(os.environ.get("FAKE_JEV_ERROR", "jev: 未找到任何 API key"), file=sys.stderr)
        return 2
    scores = json.loads(os.environ.get("FAKE_JEV_SCORES", "{}"))
    error_ids = set(json.loads(os.environ.get("FAKE_JEV_ERROR_IDS", '["b"]')))
    rows = [success(state, scores.get(state["candidate"]["id"], 3))
            for state in states]

    if mode == "reverse":
        rows.reverse()
    elif mode == "fractional":
        for row in rows:
            row["answer"].update({
                "score": 2.7,
                "value": 2.7,
                "label": "3",
                "probabilities": {"0": 0.05, "1": 0.1, "2": 0.15,
                                  "3": 0.5, "4": 0.2},
                "confidence": 0.8,
            })
    elif mode == "rounded":
        # Observed live: score/confidence and displayed probabilities have
        # independent rounding; score need not equal the rounded expectation.
        rows = [success(state, 4) for state in states]
        for row in rows:
            row["answer"].update(score=3.99, value=3.99, confidence=0.99)
    elif mode in {"mixed", "all_errors"}:
        rows = [{"input": json.dumps(row["input"], ensure_ascii=False),
                 "error": os.environ.get("FAKE_JEV_ERROR", UNTRUSTED_ERROR)}
                if mode == "all_errors" or row["input"]["candidate"]["id"] in error_ids
                else row for row in rows]
    elif mode == "missing":
        rows = rows[:1]
    elif mode == "malformed":
        if rows:
            emit(rows[0])
        print("{not-valid-json", flush=True)
        return 2
    elif mode == "unknown":
        rows = rows[:1]
        unknown = copy.deepcopy(states[-1])
        unknown["candidate"]["id"] = "not-submitted"
        rows.append(success(unknown, 4))
    elif mode == "wrong_state":
        rows[-1]["input"] = copy.deepcopy(rows[-1]["input"])
        rows[-1]["input"]["candidate"]["snippet"] = "CHANGED_UNTRUSTED_EXCERPT"
    elif mode == "no_echo":
        rows[-1].pop("input")
    elif mode == "duplicate":
        duplicate = copy.deepcopy(rows[-1])
        duplicate["answer"] = success(duplicate["input"], 1)["answer"]
        rows.append(duplicate)
    elif mode == "timeout":
        if rows:
            emit(rows[0])
        time.sleep(5)
        return 0
    elif mode == "output_limit":
        if rows:
            emit(rows[0])
        print("UNTRUSTED_OVERSIZE_RESPONSE_" + "x" * 70000, flush=True)
        return 2
    elif mode == "invalid_answer":
        mutation = os.environ.get("FAKE_JEV_INVALID_ANSWER", "score_out_of_range")
        answer = rows[-1]["answer"]
        if mutation == "score_out_of_range":
            answer["score"] = 5
        elif mutation == "huge_score":
            answer["score"] = 10 ** 400
        elif mutation == "fractional_score":
            answer["score"] = 2.5
        elif mutation == "nonfinite_score":
            answer["score"] = float("nan")
        elif mutation == "wrong_type":
            answer["type"] = "choice"
        elif mutation == "wrong_value":
            answer["value"] = 0 if answer["score"] != 0 else 4
        elif mutation == "wrong_label":
            answer["label"] = "other"
        elif mutation == "missing_answer":
            rows[-1].pop("answer")
        elif mutation == "wrong_legend":
            answer["legend"]["0"] = "unrecognized"
        elif mutation == "bad_probability":
            answer["probabilities"]["0"] = -1
        elif mutation == "inconsistent_probabilities":
            answer["probabilities"] = {str(i): float(i == 0) for i in range(5)}
            answer["label"] = "0"
        elif mutation == "unnormalized_probabilities":
            answer["probabilities"] = {str(i): 1.0 for i in range(5)}
        elif mutation == "nonfinite_confidence":
            answer["confidence"] = float("inf")
        else:
            raise ValueError("Unsupported fake answer mutation")

    for row in rows:
        emit(row)
    if mode in {"mixed", "all_errors", "exit2"}:
        print(UNTRUSTED_ERROR, file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
