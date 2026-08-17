import inspect

import pytest

from renderdesk import comments, mcp_server, shares, tools
from renderdesk.mcp_server import mcp
from renderdesk.models import ArtifactFormat

# Every @mcp.tool() wrapper paired with the implementation it delegates to.
# The wrappers are thin by design (see CLAUDE.md) — they resolve the calling
# connection and hand off — so the tests below check the one thing that
# *only* exists at this layer and is therefore invisible to the tests that
# exercise tools.py/comments.py/shares.py directly: the signature and the
# wire schema a real MCP client actually sees.
_TOOL_DELEGATIONS = (
    (mcp_server.publish_artifact, tools.publish_artifact),
    (mcp_server.update_artifact, tools.update_artifact),
    (mcp_server.get_artifact, tools.get_artifact),
    (mcp_server.list_artifacts, tools.list_artifacts),
    (mcp_server.list_comments, comments.list_comments),
    (mcp_server.reply_to_comment, comments.reply_to_comment),
    (mcp_server.resolve_comment_thread, comments.resolve_comment_thread),
    (mcp_server.share_artifact, shares.share_artifact),
)
_DELEGATION_IDS = [wrapper.__name__ for wrapper, _ in _TOOL_DELEGATIONS]


def _parameters(func) -> list[tuple[str, object]]:
    # Names and defaults only, deliberately not annotations: a wrapper
    # narrows its implementation's plain `str` to a Literal so the MCP
    # schema advertises the valid values, so the annotations are expected
    # to differ. test_format_enum_matches_artifact_format covers that
    # narrowing separately.
    return [(name, p.default) for name, p in inspect.signature(func).parameters.items()]


async def test_registered_tools_match_the_expected_set():
    registered = {tool.name for tool in await mcp.list_tools()}
    assert registered == set(_DELEGATION_IDS)


@pytest.mark.parametrize(("wrapper", "impl"), _TOOL_DELEGATIONS, ids=_DELEGATION_IDS)
def test_wrapper_signature_matches_its_implementation(wrapper, impl):
    impl_parameters = _parameters(impl)
    # The two arguments a wrapper supplies itself rather than accepting from
    # the caller — a connection must never be able to name its own.
    assert [name for name, _ in impl_parameters[:2]] == ["session", "connection_id"]
    assert _parameters(wrapper) == impl_parameters[2:]


@pytest.mark.parametrize("tool_name", ["publish_artifact", "update_artifact"])
async def test_format_enum_matches_artifact_format(tool_name):
    # The wire schema is the only place a format can go stale: _parse_format
    # accepts every ArtifactFormat member regardless, so an entry missing
    # here rejects a supported format before any of our code runs. This is
    # exactly how update_artifact silently stopped accepting "react".
    tool = next(t for t in await mcp.list_tools() if t.name == tool_name)
    schema = tool.inputSchema["properties"]["format"]
    # publish's format is required (a bare enum); update's is optional, so
    # pydantic renders it as anyOf[enum, null].
    enum = schema.get("enum") or next(option["enum"] for option in schema["anyOf"] if "enum" in option)
    assert sorted(enum) == sorted(f.value for f in ArtifactFormat)


async def test_publish_artifact_prompt_is_registered():
    prompts = await mcp.list_prompts()
    assert any(p.name == "publish_artifact" for p in prompts)


async def test_publish_artifact_prompt_renders_format_guidance():
    result = await mcp.get_prompt("publish_artifact", {"content": "hello world", "title": "Greeting"})

    assert len(result.messages) == 1
    text = result.messages[0].content.text
    assert "hello world" in text
    assert "Greeting" in text
    assert "markdown" in text
    assert "csv" in text
    assert "CSP" in text
    assert "react" in text


async def test_publish_artifact_prompt_defaults_title_to_inference_hint():
    result = await mcp.get_prompt("publish_artifact", {"content": "hello world"})

    text = result.messages[0].content.text
    assert "infer a short, descriptive title" in text


async def test_upload_large_artifact_prompt_is_registered():
    prompts = await mcp.list_prompts()
    assert any(p.name == "upload_large_artifact" for p in prompts)


async def test_upload_large_artifact_prompt_renders_curl_recipe():
    result = await mcp.get_prompt(
        "upload_large_artifact",
        {"content_path": "/tmp/page.html", "format": "html", "title": "Demo"},
    )

    assert len(result.messages) == 1
    text = result.messages[0].content.text
    assert "/tmp/page.html" in text
    assert '"format":"html"' not in text  # sanity: not accidentally double-encoded
    assert "format:\"html\"" in text
    assert "title:\"Demo\"" in text
    assert "/mcp/" in text
    # Regression for CLAUDE-SECURITY-RESULTS.md F18: the token must never
    # land in a curl argv — it's passed via -K/--config instead of -H.
    assert "-H \"Authorization" not in text
    assert "-K " in text
    assert "mktemp" in text
    assert "__" not in text  # no leftover template placeholders


async def test_upload_large_artifact_prompt_defaults_title_to_inference_hint():
    result = await mcp.get_prompt("upload_large_artifact", {"content_path": "/tmp/page.html"})

    text = result.messages[0].content.text
    assert "infer a short, descriptive title" in text
    assert 'format:"html"' in text
