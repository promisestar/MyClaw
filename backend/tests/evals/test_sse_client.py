"""SSE 客户端分帧与解析单测。"""

from evals.agent.harness.sse_client import _drain_sse_blocks, _parse_sse_block


def test_parse_sse_block_crlf():
    block = (
        "event: chunk\r\n"
        'data: {"content":"hi"}\r\n'
    )
    parsed = _parse_sse_block(block)
    assert parsed is not None
    assert parsed["event"] == "chunk"
    assert parsed["data"]["content"] == "hi"


def test_drain_sse_blocks_crlf_separator():
    raw = (
        "event: session\r\n"
        'data: {"session_id":"s1"}\r\n'
        "\r\n"
        "event: done\r\n"
        'data: {"content":"ok","session_id":"s1"}\r\n'
        "\r\n"
    )
    blocks, remainder = _drain_sse_blocks(raw)
    assert remainder == ""
    assert [b["event"] for b in blocks] == ["session", "done"]
    assert blocks[1]["data"]["content"] == "ok"


def test_drain_sse_blocks_lf_separator():
    raw = (
        "event: error\n"
        'data: {"error":"boom"}\n'
        "\n"
    )
    blocks, remainder = _drain_sse_blocks(raw)
    assert remainder == ""
    assert blocks[0]["event"] == "error"
    assert blocks[0]["data"]["error"] == "boom"


def test_drain_sse_blocks_partial_remainder():
    raw = (
        "event: chunk\r\n"
        'data: {"content":"part"}\r\n'
        "\r\n"
        "event: chunk\r\n"
        'data: {"content":"ial"}'
    )
    blocks, remainder = _drain_sse_blocks(raw)
    assert len(blocks) == 1
    assert blocks[0]["data"]["content"] == "part"
    assert 'event: chunk' in remainder
