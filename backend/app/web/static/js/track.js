/* Employee time tracker.
 *
 * Every button posts to the server and renders whatever comes back, so the
 * page never holds an opinion about state that the server disagrees with.
 *
 * The only signal sent from here is "this page is open". A browser cannot see
 * input outside its own tab, so no idle time is reported — claiming otherwise
 * would mean alerting a team leader that someone is inactive while they are
 * working in another application.
 */
(function () {
  "use strict";

  var root = document.getElementById("tracker");
  if (!root) return;

  var el = {
    emoji: root.querySelector("[data-emoji]"),
    state: root.querySelector("[data-state]"),
    detail: root.querySelector("[data-detail]"),
    countdown: root.querySelector("[data-countdown]"),
    clockIn: root.querySelector("[data-clock-in]"),
    worked: root.querySelector("[data-worked]"),
    breaks: root.querySelector("[data-breaks]"),
    conn: root.querySelector("[data-conn]"),
    msg: root.querySelector("[data-msg]"),
    allowance: root.querySelector("[data-allowance]"),
    clockBtn: root.querySelector('[data-action="clock"]'),
    lunchBtn: root.querySelector('[data-action="lunch"]'),
    shortBtn: root.querySelector('[data-action="short"]'),
    endBreakBtn: root.querySelector('[data-action="end-break"]'),
  };

  var EMOJI = { active: "🟢", on_break: "🟡", idle: "🟠", offline: "🔴", clocked_out: "⚫" };
  var state = null;
  var breakRemaining = null;
  var busy = false;

  function duration(seconds) {
    seconds = Math.max(0, Math.round(seconds || 0));
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    if (h) return h + "h " + m + "m";
    if (m) return m + "m";
    return seconds + "s";
  }

  function post(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(body || {}),
    }).then(function (response) {
      if (response.status === 401 || response.status === 403) {
        window.location.href = "/login?next=/track";
        throw new Error("signed out");
      }
      return response.json().then(function (data) {
        if (!response.ok) throw new Error(data.detail || "Something went wrong");
        return data;
      });
    });
  }

  function say(text, kind) {
    el.msg.textContent = text || "";
    el.msg.className = "tracker-msg" + (kind ? " " + kind : "");
  }

  function setConnection(online) {
    el.conn.textContent = online ? "Connected" : "Not connected";
    el.conn.className = "conn " + (online ? "ok" : "bad");
  }

  function render(data) {
    state = data;
    breakRemaining = data.break ? data.break.remaining_seconds : null;

    var clockedIn = data.state !== "clocked_out";
    var onBreak = data.state === "on_break";

    el.emoji.textContent = EMOJI[data.state] || "⚫";
    el.state.textContent = data.label || "";
    // During a break the countdown pill below already states the time left, so
    // repeating it here just says the same thing twice.
    el.detail.textContent = onBreak ? "" : data.detail || "";

    el.clockIn.textContent = data.clock_in_display || "—";
    el.worked.textContent = clockedIn ? duration(data.worked_seconds) : "—";
    el.breaks.textContent = clockedIn ? duration(data.break_seconds) : "—";

    el.clockBtn.textContent = clockedIn ? "Clock Out" : "Clock In";
    el.clockBtn.classList.toggle("secondary", clockedIn);
    el.clockBtn.dataset.clockState = clockedIn ? "in" : "out";
    // act() disables every button while a request is in flight, so render()
    // has to re-enable each one explicitly — anything it forgets stays dead.
    el.clockBtn.disabled = busy;
    el.endBreakBtn.disabled = busy;

    var used = data.breaks_used || { lunch: 0, short: 0 };
    var allow = data.allowances || {};
    var lunchLeft = Math.max(0, (allow.lunch_per_day || 0) - used.lunch);
    var shortLeft = Math.max(0, (allow.short_per_day || 0) - used.short);

    el.lunchBtn.hidden = onBreak;
    el.shortBtn.hidden = onBreak;
    el.endBreakBtn.hidden = !onBreak;

    el.lunchBtn.disabled = busy || !clockedIn || onBreak || lunchLeft === 0;
    el.shortBtn.disabled = busy || !clockedIn || onBreak || shortLeft === 0;
    el.lunchBtn.textContent = "Start Lunch Break (" + lunchLeft + " left)";
    el.shortBtn.textContent = "Start 10-Minute Break (" + shortLeft + " left)";
    el.endBreakBtn.textContent =
      data.break && data.break.type === "lunch" ? "End Lunch Break" : "End Break";

    el.allowance.textContent =
      "Lunch " + duration(allow.lunch_seconds) +
      " · short break " + duration(allow.short_seconds) +
      ". Start a break before you step away and no alert is sent.";

    tickCountdown();
    setConnection(true);
  }

  function tickCountdown() {
    if (breakRemaining === null || !state || state.state !== "on_break") {
      el.countdown.hidden = true;
      return;
    }
    var label = state.break && state.break.type === "lunch" ? "Lunch" : "Break";
    el.countdown.hidden = false;
    if (breakRemaining >= 0) {
      el.countdown.textContent = label + ": " + duration(breakRemaining) + " left";
      el.countdown.classList.remove("over");
    } else {
      el.countdown.textContent = label + ": " + duration(-breakRemaining) + " over";
      el.countdown.classList.add("over");
    }
    breakRemaining -= 1;
  }

  function act(path, body, pending) {
    if (busy) return;
    busy = true;
    say(pending);
    var buttons = root.querySelectorAll("button");
    buttons.forEach(function (b) { b.disabled = true; });

    post(path, body)
      .then(function (data) {
        render(data);
        say("", "");
      })
      .catch(function (error) {
        // A refusal from the server is the authoritative answer — show it as is.
        say(error.message, "error");
        setConnection(false);
      })
      .finally(function () {
        busy = false;
        if (state) render(state);
      });
  }

  el.clockBtn.addEventListener("click", function () {
    if (el.clockBtn.dataset.clockState === "in") {
      if (!window.confirm("Clock out and end your working day?")) return;
      act("/api/v1/me/clock-out", {}, "Clocking out…");
    } else {
      act("/api/v1/me/clock-in", {}, "Clocking in…");
    }
  });

  el.lunchBtn.addEventListener("click", function () {
    act("/api/v1/me/break/start", { break_type: "lunch" }, "Starting lunch…");
  });
  el.shortBtn.addEventListener("click", function () {
    act("/api/v1/me/break/start", { break_type: "short" }, "Starting break…");
  });
  el.endBreakBtn.addEventListener("click", function () {
    act("/api/v1/me/break/end", {}, "Ending break…");
  });

  /* ---- Heartbeat --------------------------------------------------------
   * Browsers throttle timers in background tabs and may suspend them
   * entirely, so a missed beat is expected and is not treated as a problem.
   * A beat is also sent whenever the tab becomes visible again, which
   * recovers the display immediately rather than after the next interval.
   */
  function beat() {
    post("/api/v1/me/heartbeat", { page_visible: !document.hidden })
      .then(render)
      .catch(function () { setConnection(false); });
  }

  var interval = (parseInt(root.dataset.heartbeat, 10) || 45) * 1000;
  setInterval(beat, interval);
  setInterval(tickCountdown, 1000);

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) beat();
  });

  // Warn before closing mid-shift: the tab closing is harmless, but people
  // assume it clocks them out, and it does not.
  window.addEventListener("beforeunload", function (event) {
    if (state && state.state !== "clocked_out") {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  beat();
})();
