// Runs a React artifact with no bundler: the JSX/TSX source sits inertly in
// a type="application/json" script tag (view.py JSON-escapes it so a literal
// "</script" inside the source can't end the tag early), Babel standalone
// transpiles it in-browser, and a tiny CommonJS shim resolves each entry in
// LIBRARY_GLOBALS below to its vendored global — anything else the artifact
// imports fails with a readable error instead of a silent blank page, since
// there's no bundler here to fetch a real npm package. view.py's
// _optional_react_assets decides, per artifact, which of these <script>
// tags actually get loaded (based on which specifiers the source imports),
// so this table can safely list more specifiers than any given artifact
// has scripts for — the LIBRARY_GLOBALS[name] === undefined check below is
// what turns "the script wasn't loaded" into the same readable error as
// "the specifier isn't supported at all", rather than a raw
// ReferenceError/TypeError from deeper in the artifact's own code.
(function () {
    var source = JSON.parse(document.getElementById("artifact-source").textContent);
    var root = document.getElementById("root");

    var LIBRARY_GLOBALS = {
        react: "React",
        "react-dom": "ReactDOM",
        "react-dom/client": "ReactDOM",
        three: "THREE",
        lodash: "_",
        d3: "d3",
        mathjs: "math",
        "chart.js": "Chart",
        "chart.js/auto": "Chart",
        tone: "Tone",
        papaparse: "Papa",
        xlsx: "XLSX",
    };

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
        var globalName = LIBRARY_GLOBALS[name];
        var value = globalName !== undefined ? window[globalName] : undefined;
        if (value === undefined) {
            throw new Error(
                'import "' + name + '" is not available. renderdesk\'s React artifacts only have ' +
                "the following in scope: " + Object.keys(LIBRARY_GLOBALS).join(", ") + "."
            );
        }
        return value;
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
