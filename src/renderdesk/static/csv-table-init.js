document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("table.csv-table").forEach(function (table) {
        var headers = table.querySelectorAll("thead th");
        if (!headers.length) {
            return;
        }

        // Freeze each column's natural (content-driven) width into an
        // explicit <colgroup> before switching to table-layout: fixed —
        // otherwise fixed layout would collapse every column to an equal
        // share of the table width instead of keeping what auto-layout
        // rendered.
        var colgroup = document.createElement("colgroup");
        var cols = [];
        headers.forEach(function (th) {
            var col = document.createElement("col");
            col.style.width = th.getBoundingClientRect().width + "px";
            colgroup.appendChild(col);
            cols.push(col);
        });
        table.insertBefore(colgroup, table.firstChild);
        table.style.tableLayout = "fixed";

        headers.forEach(function (th, i) {
            if (i === headers.length - 1) {
                return; // no handle after the last column
            }

            var handle = document.createElement("span");
            handle.className = "csv-col-resize-handle";
            th.appendChild(handle);

            handle.addEventListener("mousedown", function (e) {
                var startX = e.clientX;
                var startWidth = cols[i].getBoundingClientRect().width;
                document.body.style.userSelect = "none";

                function onMove(moveEvent) {
                    var newWidth = Math.max(40, startWidth + (moveEvent.clientX - startX));
                    cols[i].style.width = newWidth + "px";
                }

                function onUp() {
                    document.removeEventListener("mousemove", onMove);
                    document.removeEventListener("mouseup", onUp);
                    document.body.style.userSelect = "";
                }

                document.addEventListener("mousemove", onMove);
                document.addEventListener("mouseup", onUp);
                e.preventDefault();
            });
        });
    });
});
