"""P0 联调验证：usage 记录器 + 归一化 + 聚合 + enhanced_llm 辅助函数。

不依赖真实 LLM，全部用本地数据/对象验证关键路径。
运行：./.venv/Scripts/python.exe -m tests.test_p0_usage
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

# 让脚本能在不安装 pytest 的情况下直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ 根


def _obj(**kw):
    """构造一个属性式对象，模拟 OpenAI usage / chunk。"""
    return types.SimpleNamespace(**kw)


def test_normalize():
    from src.logging.llm_usage_logger import normalize_usage, empty_usage

    # OpenAI 规范：cached 在 prompt_tokens_details
    u = normalize_usage(_obj(
        prompt_tokens=200,
        completion_tokens=10,
        total_tokens=210,
        prompt_tokens_details=_obj(cached_tokens=128),
    ))
    assert u and u["cached_tokens"] == 128, u
    assert u["prompt_tokens"] == 200 and u["total_tokens"] == 210

    # MiniMax 顶层 cached_tokens
    u2 = normalize_usage({"prompt_tokens": 177, "completion_tokens": 8,
                          "total_tokens": 185, "cached_tokens": 128})
    assert u2 and u2["cached_tokens"] == 128, u2

    # 没有 usage
    assert normalize_usage(None) is None
    assert normalize_usage({}) is None
    assert empty_usage()["cached_tokens"] == 0
    print("✅ normalize_usage 通过")


def test_stream_options_detection():
    from src.agent.enhanced_llm import _is_stream_options_unsupported

    class _FakeBadRequest(Exception):
        status_code = 400

        def __str__(self):
            return "invalid_request_error: bad"

    # 400 状态码即可命中降级
    assert _is_stream_options_unsupported(_FakeBadRequest())
    # 文案命中
    assert _is_stream_options_unsupported(Exception("Unrecognized request argument: stream_options"))
    assert _is_stream_options_unsupported(Exception("include_usage is not supported"))
    # 正常错误不命中
    assert not _is_stream_options_unsupported(Exception("connection reset"))
    print("✅ _is_stream_options_unsupported 通过")


def test_logger_roundtrip(tmp_dir: Path):
    # 重定向日志目录到临时目录
    os.environ["TOOL_LOG_DIR"] = str(tmp_dir)
    from src.logging.tool_logger import ToolCallLogger, set_trace_id
    from src.logging.llm_usage_logger import (
        LLMUsageLogger, set_session_id, empty_usage, merge_usage,
    )
    ToolCallLogger._log_dir = tmp_dir  # 复用同一目录

    set_trace_id("t-abc")
    set_session_id("sess-1")

    # 写两条主循环 + 一条无 usage + 一条子代理摘要
    LLMUsageLogger.log(model="glm-4", call_site="main_loop",
                      usage={"prompt_tokens": 1000, "completion_tokens": 50,
                             "total_tokens": 1050, "cached_tokens": 800,
                             "reasoning_tokens": 0},
                      duration_ms=1200, stream=True, iteration=1)
    LLMUsageLogger.log(model="glm-4", call_site="main_loop",
                      usage={"prompt_tokens": 2000, "completion_tokens": 80,
                             "total_tokens": 2080, "cached_tokens": 1800,
                             "reasoning_tokens": 0},
                      duration_ms=900, stream=True, iteration=2)
    LLMUsageLogger.log(model="glm-4", call_site="subagent_summary",
                      usage=None, duration_ms=300, stream=False)
    # log_response 模拟
    resp = _obj(usage=_obj(prompt_tokens=500, completion_tokens=20,
                           total_tokens=520, cached_tokens=0),
                model="glm-4")
    LLMUsageLogger.log_response(resp, call_site="context_compress")

    files = LLMUsageLogger.list_files()
    assert len(files) == 1, files
    assert files[0]["entry_count"] == 4, files

    # 单日聚合
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    day = LLMUsageLogger.daily_summary(today)
    assert day["calls"] == 4, day
    assert day["calls_with_usage"] == 3, day
    assert day["usage_coverage"] == 0.75, day
    assert day["prompt_tokens"] == 3500, day  # 1000+2000+500
    assert day["cached_tokens"] == 2600, day  # 800+1800+0
    assert 0.74 < day["cache_hit_rate"] < 0.75, day  # round(2600/3500, 4)=0.7429
    # 按模型：只有 glm-4
    assert len(day["by_model"]) == 1 and day["by_model"][0]["calls"] == 4
    # 按调用点：3 个
    sites = {b["name"] for b in day["by_call_site"]}
    assert sites == {"main_loop", "subagent_summary", "context_compress"}, sites

    # 最近记录
    recent = LLMUsageLogger.recent(today, limit=2)
    assert len(recent) == 2 and recent[-1]["call_site"] == "context_compress"

    # 多日汇总
    rng = LLMUsageLogger.range_summary(3)
    assert rng["calls"] == 4 and len(rng["by_day"]) == 3, rng

    print("✅ LLMUsageLogger 写入/聚合/查询 通过")


def test_merge_usage():
    from src.logging.llm_usage_logger import empty_usage, merge_usage
    acc = empty_usage()
    merge_usage(acc, {"prompt_tokens": 100, "completion_tokens": 10,
                      "total_tokens": 110, "cached_tokens": 80})
    merge_usage(acc, {"prompt_tokens": 50, "completion_tokens": 5,
                      "total_tokens": 55, "cached_tokens": 40})
    assert acc["prompt_tokens"] == 150 and acc["cached_tokens"] == 120
    print("✅ merge_usage 通过")


def main():
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="usage_test_"))
    test_normalize()
    test_stream_options_detection()
    test_merge_usage()
    test_logger_roundtrip(tmp)
    print("\n全部 P0 联调验证通过 ✅")
    print(f"临时日志目录: {tmp}")


if __name__ == "__main__":
    main()
