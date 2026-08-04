document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("table.sortable-table").forEach(function (table) {
        var headerRow = table.tHead && table.tHead.rows[0];
        var tbody = table.tBodies[0];
        if (!headerRow || !tbody) {
            return;
        }

        var state = { index: null, dir: 1 };

        function cellValue(row, index) {
            var cell = row.cells[index];
            if (!cell) {
                return "";
            }
            var raw = cell.hasAttribute("data-sort-value") ? cell.getAttribute("data-sort-value") : cell.textContent;
            return raw.trim().toLowerCase();
        }

        function setIndicator(index, dir) {
            Array.prototype.forEach.call(headerRow.cells, function (th, i) {
                th.classList.remove("sort-asc", "sort-desc");
                if (i === index) {
                    th.classList.add(dir === 1 ? "sort-asc" : "sort-desc");
                }
            });
        }

        function applySort(index, dir) {
            var rows = Array.prototype.slice.call(tbody.rows);
            rows.sort(function (a, b) {
                var av = cellValue(a, index);
                var bv = cellValue(b, index);
                if (av < bv) return -1 * dir;
                if (av > bv) return 1 * dir;
                return 0;
            });
            rows.forEach(function (row) {
                tbody.appendChild(row);
            });
            state = { index: index, dir: dir };
            setIndicator(index, dir);
        }

        Array.prototype.forEach.call(headerRow.cells, function (th, index) {
            if (!th.textContent.trim()) {
                return; // skip empty (actions) header
            }
            th.classList.add("sortable-col");
            th.tabIndex = 0;
            th.setAttribute("role", "button");
            th.addEventListener("click", function () {
                var dir = state.index === index ? -state.dir : 1;
                applySort(index, dir);
            });
            th.addEventListener("keydown", function (e) {
                if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    th.click();
                }
            });
        });

        // Rows already arrive from the server in the declared default order —
        // just mark the indicator without re-sorting, so a stable tie-break
        // (e.g. secondary ordering) isn't disturbed.
        var defaultIndex = parseInt(table.getAttribute("data-default-sort-index"), 10);
        if (!isNaN(defaultIndex)) {
            var defaultDir = table.getAttribute("data-default-sort-dir") === "asc" ? 1 : -1;
            state = { index: defaultIndex, dir: defaultDir };
            setIndicator(defaultIndex, defaultDir);
        }
    });
});
