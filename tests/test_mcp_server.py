from renderdesk.mcp_server import mcp


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
