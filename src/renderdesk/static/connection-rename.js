document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form.rename-form").forEach(function (form) {
        var input = form.querySelector("input[name=label]");
        var button = form.querySelector("button[type=submit]");
        if (!input || !button) {
            return;
        }
        function sync() {
            button.hidden = input.value === input.defaultValue;
        }
        input.addEventListener("input", sync);
        sync();
    });
});
