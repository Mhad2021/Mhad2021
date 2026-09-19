/* Presence dashboard client.
 *
 * No framework and no CDN: this runs on internal networks that often block
 * outbound CDN requests, and a locked-down browser policy should not be able
 * to break the live board.
 */
(function () {
  "use strict";

  /* ---------- Live region refresh -------------------------------------- *
   * Any element with data-refresh="<url>" data-interval="<seconds>" has its
   * innerHTML replaced with that URL's response on a timer. Refresh pauses
   * while the tab is hidden so idle tabs do not hammer the server.
   */
  function startRefresher(el) {
    var url = el.getAttribute("data-refresh");
    var interval = (parseInt(el.getAttribute("data-interval"), 10) || 10) * 1000;
    var stamp = document.querySelector("[data-refresh-stamp]");
    var failures = 0;

    function tick() {
      if (document.hidden) return;
      fetch(url, {
        headers: { "X-Requested-With": "fetch" },
        credentials: "same-origin",
      })
        .then(function (r) {
          if (r.status === 401 || r.status === 403) {
            window.location.href = "/login";
            throw new Error("signed out");
          }
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.text();
        })
        .then(function (html) {
          el.innerHTML = html;
          failures = 0;
          if (stamp) {
            stamp.textContent = "Updated " + new Date().toLocaleTimeString();
            stamp.classList.remove("badge", "bad");
          }
        })
        .catch(function () {
          failures += 1;
          if (stamp && failures >= 2) {
            stamp.textContent = "Reconnecting…";
            stamp.classList.add("badge", "bad");
          }
        });
    }

    setInterval(tick, interval);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) tick();
    });
  }

  /* ---------- Live duration counters ------------------------------------ *
   * Elements with data-since="<unix seconds>" tick upward between refreshes,
   * so "Idle for 9m" does not sit frozen while waiting for the next poll.
   */
  function formatDuration(seconds) {
    if (seconds < 60) return seconds + "s";
    var m = Math.floor(seconds / 60);
    if (m < 60) return m + "m";
    return Math.floor(m / 60) + "h " + (m % 60) + "m";
  }

  function tickDurations() {
    var now = Date.now() / 1000;
    document.querySelectorAll("[data-since]").forEach(function (el) {
      var since = parseFloat(el.getAttribute("data-since"));
      if (!isNaN(since)) {
        el.textContent = formatDuration(Math.max(0, Math.floor(now - since)));
      }
    });
  }

  /* ---------- JSON form submission -------------------------------------- *
   * Progressive enhancement over the REST API: forms with data-api post JSON
   * and show the result inline instead of navigating away.
   */
  function bindApiForms(root) {
    (root || document).querySelectorAll("form[data-api]").forEach(function (form) {
      if (form.dataset.bound) return;
      form.dataset.bound = "1";

      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var button = form.querySelector("[type=submit]");
        var status = form.querySelector("[data-status]");
        var payload = {};
        var config = {};

        new FormData(form).forEach(function (value, key) {
          if (value === "") return;
          // Fields named cfg__<key> are collapsed into a nested `config` object,
          // which is the shape the integration endpoints expect.
          if (key.indexOf("cfg__") === 0) {
            config[key.slice(5)] = value;
            return;
          }
          var field = form.elements[key];
          if (field && field.type === "number") payload[key] = Number(value);
          else if (value === "true") payload[key] = true;
          else if (value === "false") payload[key] = false;
          else payload[key] = value;
        });

        form.querySelectorAll("input[type=checkbox]").forEach(function (box) {
          if (box.name && box.name.indexOf("cfg__") !== 0) payload[box.name] = box.checked;
        });

        if (Object.keys(config).length) payload.config = config;

        if (button) button.disabled = true;
        if (status) { status.className = "small muted"; status.textContent = "Saving…"; }

        fetch(form.getAttribute("data-api"), {
          method: form.getAttribute("data-method") || "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify(payload),
        })
          .then(function (r) {
            return r.json().then(function (body) { return { ok: r.ok, body: body }; });
          })
          .then(function (result) {
            if (button) button.disabled = false;
            if (!result.ok) {
              if (status) {
                status.className = "small";
                status.style.color = "var(--red)";
                status.textContent = result.body.detail || "Something went wrong";
              }
              return;
            }
            if (form.dataset.reload === "true") {
              window.location.reload();
              return;
            }
            if (status) {
              status.className = "small";
              status.style.color = "var(--green)";
              status.textContent = result.body.detail || "Saved";
            }
            if (form.dataset.result) {
              var target = document.querySelector(form.dataset.result);
              if (target) {
                target.hidden = false;
                target.textContent = JSON.stringify(result.body, null, 2);
              }
            }
          })
          .catch(function () {
            if (button) button.disabled = false;
            if (status) {
              status.className = "small";
              status.style.color = "var(--red)";
              status.textContent = "Could not reach the server";
            }
          });
      });
    });
  }

  /* ---------- Confirm-before-action ------------------------------------- */
  function bindConfirms() {
    document.querySelectorAll("[data-confirm]").forEach(function (el) {
      el.addEventListener("click", function (event) {
        if (!window.confirm(el.getAttribute("data-confirm"))) event.preventDefault();
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-refresh]").forEach(startRefresher);
    bindApiForms();
    bindConfirms();
    setInterval(tickDurations, 1000);
    tickDurations();
  });
})();
