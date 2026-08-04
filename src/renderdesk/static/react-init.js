// Runs a React artifact with no bundler: the JSX/TSX source sits inertly in
// a type="application/json" script tag (view.py JSON-escapes it so a literal
// "</script" inside the source can't end the tag early), Babel standalone
// transpiles it in-browser, and a tiny CommonJS shim resolves only "react"/
// "react-dom" to the vendored globals below — anything else the artifact
// imports fails with a readable error instead of a silent blank page, since
// there's no bundler here to fetch a real npm package.
(function () {
    var source = JSON.parse(document.getElementById("artifact-source").textContent);
    var root = document.getElementById("root");

    function showError(err) {
        var pre = document.createElement("pre");
        pre.style.cssText =
            "white-space:pre-wrap;color:#b91c1c;font-family:ui-monospace,Menlo,Consolas,monospace;" +
            "font-size:0.85rem;padding:1rem;margin:0";
        pre.textContent = "renderdesk could not run this React artifact:\n\n" + (err && err.message ? err.message : err);
        root.textContent = "";
        root.appendChild(pre);
    }

    function requireShim(name) {
        if (name === "react") return React;
        if (name === "react-dom" || name === "react-dom/client") return ReactDOM;
        throw new Error(
            'import "' + name + '" is not available. renderdesk\'s React artifacts only have ' +
            "react and react-dom in scope — no other packages can be imported."
        );
    }

    try {
        var transformed = Babel.transform(source, {
            filename: "artifact.tsx",
            presets: [["react", { runtime: "classic" }], "typescript"],
            plugins: ["transform-modules-commonjs"],
        }).code;

        var moduleObj = { exports: {} };
        new Function("require", "module", "exports", transformed)(requireShim, moduleObj, moduleObj.exports);

        var Component = moduleObj.exports.default || moduleObj.exports;
        if (typeof Component !== "function") {
            throw new Error("This artifact must have a default export that is a React component.");
        }

        ReactDOM.createRoot(root).render(React.createElement(Component));
    } catch (err) {
        showError(err);
    }
})();
