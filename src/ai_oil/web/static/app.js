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
