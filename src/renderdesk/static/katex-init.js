document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".math.inline, .math.block").forEach(function (el) {
        try {
            // renderToString, not render(el) — the latter has a
            // document.compatMode check baked in that permanently
            // replaces itself with a function that always throws if the
            // page isn't (for whatever reason) in standards mode, which
            // silently broke every equation on a page at once.
            // renderToString has no such gate.
            el.innerHTML = katex.renderToString(el.textContent, {
                throwOnError: false,
                displayMode: el.classList.contains("block"),
            });
        } catch (e) {
            // Leave the raw text in place on unexpected failure.
        }
    });
});
