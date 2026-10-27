document.addEventListener("htmx:responseError", function (event) {
  var flash = document.getElementById("flash");
  if (flash) {
    flash.innerHTML = event.detail.xhr.responseText;
  }
});

document.addEventListener("htmx:afterRequest", function (event) {
  var tab = event.target.closest ? event.target.closest('[role="tab"]') : null;
  if (!tab || !event.detail.successful) {
    return;
  }
  tab.parentElement.querySelectorAll('[role="tab"]').forEach(function (other) {
    other.setAttribute("aria-selected", other === tab ? "true" : "false");
  });
});

document.addEventListener("htmx:configRequest", function (event) {
  var token = event.target.getAttribute ? event.target.getAttribute("data-path-from-value") : null;
  if (token) {
    event.detail.path = event.detail.path.replace(token, encodeURIComponent(event.target.value));
  }
});

/* Keyboard shortcuts (UI-4). Ignored while typing and when Ctrl, Alt or Meta is held; Shift is allowed
   because "/" needs it on some keyboard layouts. */
(function () {
  var SELECTED = "kb-selected";

  function isTyping(element) {
    if (!element || !element.tagName) {
      return false;
    }
    var tag = element.tagName.toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || element.isContentEditable === true;
  }

  function issueRows() {
    return Array.prototype.slice.call(document.querySelectorAll("table.issues tbody tr[data-issue-key]"));
  }

  function moveSelection(step) {
    var rows = issueRows();
    if (!rows.length) {
      return;
    }
    var current = rows.findIndex(function (row) {
      return row.classList.contains(SELECTED);
    });
    var next = current < 0 ? 0 : Math.min(rows.length - 1, Math.max(0, current + step));
    rows.forEach(function (row, index) {
      row.classList.toggle(SELECTED, index === next);
    });
    rows[next].scrollIntoView({ block: "nearest" });
  }

  function openSelected(event) {
    var row = document.querySelector("table.issues tr." + SELECTED + "[data-issue-key]");
    if (!row || (event.target.closest && event.target.closest("a, button, summary"))) {
      return false;
    }
    window.location.href = "/issues/" + encodeURIComponent(row.getAttribute("data-issue-key"));
    return true;
  }

  function focusComment() {
    var field = document.getElementById("comment-body");
    if (field) {
      field.focus();
      return;
    }
    var tab = document.getElementById("tab-comments");
    if (!tab) {
      return;
    }
    document.body.addEventListener(
      "htmx:afterSettle",
      function focusLoaded() {
        var loaded = document.getElementById("comment-body");
        if (loaded) {
          document.body.removeEventListener("htmx:afterSettle", focusLoaded);
          loaded.focus();
        }
      }
    );
    tab.click();
  }

  document.addEventListener("keydown", function (event) {
    if (event.ctrlKey || event.altKey || event.metaKey || event.defaultPrevented || isTyping(event.target)) {
      return;
    }
    var handled = false;
    switch (event.key) {
      case "c":
        window.location.href = "/issues/new";
        handled = true;
        break;
      case "/":
        var search = document.getElementById("global-search");
        if (search) {
          search.focus();
          handled = true;
        }
        break;
      case "j":
        moveSelection(1);
        handled = true;
        break;
      case "k":
        moveSelection(-1);
        handled = true;
        break;
      case "Enter":
        handled = openSelected(event);
        break;
      case "e":
        var edit = document.getElementById("edit-summary");
        if (edit) {
          edit.click();
          handled = true;
        }
        break;
      case "m":
        focusComment();
        handled = true;
        break;
    }
    if (handled) {
      event.preventDefault();
    }
  });
})();
