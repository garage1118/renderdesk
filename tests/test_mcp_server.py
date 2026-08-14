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
    assert "Mcp-Session-Id" in text
    assert "__" not in text  # no leftover template placeholders


async def test_upload_large_artifact_prompt_defaults_title_to_inference_hint():
    result = await mcp.get_prompt("upload_large_artifact", {"content_path": "/tmp/page.html"})

    text = result.messages[0].content.text
    assert "infer a short, descriptive title" in text
    assert 'format:"html"' in text
