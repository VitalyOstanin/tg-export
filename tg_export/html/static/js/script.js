/* tg-export: client-side interactivity */

document.addEventListener("DOMContentLoaded", function() {
  // Spoiler reveal on click
  document.querySelectorAll(".spoiler").forEach(function(el) {
    el.addEventListener("click", function() {
      this.classList.toggle("revealed");
    });
  });

  // Smooth scroll to message by anchor
  if (window.location.hash) {
    var target = document.querySelector(window.location.hash);
    if (target) {
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      target.classList.add("highlighted");
      setTimeout(function() { target.classList.remove("highlighted"); }, 2000);
    }
  }

  // Reply click -> scroll to original message
  document.querySelectorAll(".reply-block[data-msg-id]").forEach(function(el) {
    el.style.cursor = "pointer";
    el.addEventListener("click", function() {
      var msgId = this.getAttribute("data-msg-id");
      var target = document.getElementById("message" + msgId);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "center" });
        target.classList.add("highlighted");
        setTimeout(function() { target.classList.remove("highlighted"); }, 2000);
      }
    });
  });
});

/* Highlight animation via CSS */
var style = document.createElement("style");
style.textContent = ".highlighted { background: rgba(51,144,236,0.12); transition: background 0.5s; border-radius: 8px; }";
document.head.appendChild(style);
