document.addEventListener("htmx:responseError", function (event) {
  var flash = document.getElementById("flash");
  if (flash) {
    flash.innerHTML = event.detail.xhr.responseText;
  }
});
