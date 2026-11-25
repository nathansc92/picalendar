function localISODate(value = new Date()) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

async function request(path, options = {}) {
  const opts = { ...options };
  if (opts.body !== undefined) {
    opts.body = JSON.stringify(opts.body);
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  }
  const response = await fetch(path, opts);
  if (response.status === 204) return null;
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (_error) {
      data = null;
    }
  }
  if (!response.ok) {
    let message = "Request failed.";
    if (data && typeof data.detail === "string") message = data.detail;
    else if (data && Array.isArray(data.detail) && data.detail[0]) {
      message = data.detail[0].msg || message;
    }
    throw new Error(message);
  }
  return data;
}

function blankChore() {
  return { id: null, title: "", notes: "", family_member_id: "", due_date: "" };
}

function blankAsset() {
  return { id: null, name: "", location: "", notes: "" };
}

function blankTask() {
  return {
    id: null,
    asset_id: "",
    title: "",
    notes: "",
    interval_value: "90",
    interval_unit: "days",
    last_done_date: localISODate(),
  };
}

function parseISODate(iso) {
  const [year, month, day] = iso.slice(0, 10).split("-").map(Number);
  return new Date(year, month - 1, day);
}

function addDaysISO(iso, days) {
  const date = parseISODate(iso);
  date.setDate(date.getDate() + days);
  return localISODate(date);
}

function daysBetween(startISO, endISO) {
  const start = parseISODate(startISO);
  const end = parseISODate(endISO);
  return Math.round((end - start) / 86400000);
}

function addMinutes(dateISO, minutes, delta) {
  const date = parseISODate(dateISO);
  date.setHours(0, 0, 0, 0);
  date.setMinutes(minutes + delta);
  return {
    date: localISODate(date),
    minutes: date.getHours() * 60 + date.getMinutes(),
  };
}

function spanMinutes(startDate, startMin, endDate, endMin) {
  const start = parseISODate(startDate);
  start.setHours(0, startMin, 0, 0);
  const end = parseISODate(endDate);
  end.setHours(0, endMin, 0, 0);
  return Math.round((end - start) / 60000);
}

function combineLocal(dateISO, minutes) {
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  return `${dateISO}T${String(hours).padStart(2, "0")}:${String(mins).padStart(2, "0")}:00`;
}

function minutesFromISO(value) {
  const time = (value.split("T")[1] || "00:00:00").slice(0, 5);
  const [hours, mins] = time.split(":").map(Number);
  return hours * 60 + mins;
}

function minutesLabel(minutes) {
  const normalized = ((minutes % 1440) + 1440) % 1440;
  let hours = Math.floor(normalized / 60);
  const mins = normalized % 60;
  const suffix = hours >= 12 ? "PM" : "AM";
  hours %= 12;
  if (hours === 0) hours = 12;
  return `${hours}:${String(mins).padStart(2, "0")} ${suffix}`;
}

function localDateTimeISO(value) {
  const pad = (number) => String(number).padStart(2, "0");
  const offset = -value.getTimezoneOffset();
  const sign = offset >= 0 ? "+" : "-";
  const absolute = Math.abs(offset);
  return (
    `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}` +
    `T${pad(value.getHours())}:${pad(value.getMinutes())}:${pad(value.getSeconds())}` +
    `${sign}${pad(Math.floor(absolute / 60))}:${pad(absolute % 60)}`
  );
}

function textOnColor(hex) {
  if (!hex || hex.length < 7) return "#ffffff";
  const value = Number.parseInt(hex.slice(1, 7), 16);
  if (Number.isNaN(value)) return "#ffffff";
  const red = (value >> 16) & 255;
  const green = (value >> 8) & 255;
  const blue = value & 255;
  const luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 255;
  return luminance > 0.62 ? "#1c1917" : "#ffffff";
}

function alignWeeklyByday(rrule, dateISO) {
  if (!rrule || !/BYDAY=/i.test(rrule)) return rrule;
  const match = rrule.match(/BYDAY=([^;]+)/i);
  if (!match || match[1].includes(",")) return rrule;
  const codes = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"];
  const code = codes[parseISODate(dateISO).getDay()];
  return rrule.replace(/BYDAY=[^;]+/i, `BYDAY=${code}`);
}

function blankEvent(date, allDay) {
  const start = date ? new Date(date) : new Date();
  if (!date) start.setHours(9, 0, 0, 0);
  start.setSeconds(0, 0);
  start.setMinutes(start.getMinutes() - (start.getMinutes() % 15));
  const end = new Date(start.getTime() + 60 * 60000);
  return {
    id: null,
    title: "",
    description: "",
    member_id: "",
    calendar_id: null,
    all_day: !!allDay,
    start_date: localISODate(start),
    end_date: localISODate(allDay ? start : end),
    start_minutes: start.getHours() * 60 + start.getMinutes(),
    end_minutes: end.getHours() * 60 + end.getMinutes(),
    rrule: null,
    series_start: null,
    series_end: null,
    occurrence_date: localISODate(start),
  };
}

function formFromEvent(row) {
  const allDay = !!row.all_day;
  const startDate = row.start_time.slice(0, 10);
  let endDate = startDate;
  let startMinutes = 9 * 60;
  let endMinutes = 10 * 60;
  if (allDay) {
    endDate = addDaysISO(row.end_time.slice(0, 10), -1);
    if (endDate < startDate) endDate = startDate;
  } else {
    startMinutes = minutesFromISO(row.start_time);
    endMinutes = minutesFromISO(row.end_time);
    endDate = row.end_time.slice(0, 10);
  }
  return {
    id: row.id,
    title: row.title || "",
    description: row.description || "",
    member_id: row.member_id == null ? "" : String(row.member_id),
    calendar_id: row.calendar_id || null,
    all_day: allDay,
    start_date: startDate,
    end_date: endDate,
    start_minutes: startMinutes,
    end_minutes: endMinutes,
    rrule: row.rrule || null,
    series_start: row.series_start || row.start_time,
    series_end: row.series_end || row.end_time,
    occurrence_date: startDate,
  };
}

function eventPayloadFromForm(form) {
  let startDate = form.start_date;
  let endDate = form.end_date;
  let startMinutes = form.start_minutes;
  let endMinutes = form.end_minutes;
  let rrule = form.rrule || null;

  if (rrule && form.series_start) {
    const seriesDate = form.series_start.slice(0, 10);
    const delta = daysBetween(form.occurrence_date, form.start_date);
    startDate = addDaysISO(seriesDate, delta);
    if (form.all_day) {
      const inclusiveDays = daysBetween(form.start_date, form.end_date);
      endDate = addDaysISO(startDate, inclusiveDays);
    } else {
      const duration = spanMinutes(
        form.start_date,
        form.start_minutes,
        form.end_date,
        form.end_minutes,
      );
      const shifted = addMinutes(startDate, startMinutes, Math.max(15, duration));
      endDate = shifted.date;
      endMinutes = shifted.minutes;
    }
    rrule = alignWeeklyByday(rrule, startDate);
  }

  const payload = {
    title: form.title.trim(),
    description: form.description.trim(),
    member_id: form.member_id === "" ? null : Number(form.member_id),
    calendar_id: form.calendar_id,
    all_day: !!form.all_day,
    rrule,
  };
  if (form.all_day) {
    payload.start_time = startDate;
    payload.end_time = addDaysISO(endDate, 1);
  } else {
    payload.start_time = combineLocal(startDate, startMinutes);
    payload.end_time = combineLocal(endDate, endMinutes);
  }
  return payload;
}

const KEYBOARD_LAYOUT = {
  default: [
    "1 2 3 4 5 6 7 8 9 0 {bksp}",
    "q w e r t y u i o p",
    "a s d f g h j k l",
    "{shift} z x c v b n m - '",
    "{space} {done}",
  ],
  shift: [
    "1 2 3 4 5 6 7 8 9 0 {bksp}",
    "Q W E R T Y U I O P",
    "A S D F G H J K L",
    "{shift} Z X C V B N M - '",
    "{space} {done}",
  ],
  numeric: ["1 2 3 4 5 6 7 8 9 0 {bksp}", "{done}"],
};

document.addEventListener("alpine:init", () => {
  Alpine.data("hub", () => ({
    tab: "calendar",
    members: [],
    chores: [],
    assets: [],
    tasks: [],
    choreFilter: "all",
    loading: false,
    error: "",
    serverStartedAt: null,
    versionTimer: null,
    keyboardOpen: false,
    activeField: null,
    kb: null,
    busy: {},
    showChoreModal: false,
    showAssetModal: false,
    showTaskModal: false,
    showEventModal: false,
    showSettings: false,
    showDeleteModal: false,
    pendingDelete: null,
    choreForm: blankChore(),
    assetForm: blankAsset(),
    taskForm: blankTask(),
    eventForm: blankEvent(null, false),
    eventError: "",
    eventDeleteArmed: false,
    calendar: null,
    calendarView: "timeGridWeek",
    calendarLabel: "",
    hiddenMemberIds: [],
    showMemberFilter: false,
    showChoreFilter: false,
    now: new Date(),
    clockTimer: null,
    syncStatus: {
      online: true,
      last_synced_at: null,
      detail: "",
      linked_count: 0,
      credentials_present: true,
      syncing: false,
      loaded: false,
    },
    syncTimer: null,
    settingsError: "",
    settingsMessage: "",
    unlinkArmedId: null,

    get calendarOffline() {
      return this.syncStatus.loaded && this.syncStatus.linked_count > 0 && this.syncStatus.online === false;
    },

    get filteredChores() {
      const rows = this.chores.filter((chore) => {
        if (this.choreFilter === "all") return true;
        if (this.choreFilter === "anyone") return chore.family_member_id == null;
        return chore.family_member_id === this.choreFilter;
      });
      return rows.slice().sort((a, b) => {
        if (a.completed !== b.completed) return a.completed - b.completed;
        const aDue = a.due_date || "9999-99-99";
        const bDue = b.due_date || "9999-99-99";
        if (aDue !== bDue) return aDue < bDue ? -1 : 1;
        return a.id - b.id;
      });
    },

    get openChoreCount() {
      return this.filteredChores.filter((chore) => !chore.completed).length;
    },

    get sortedTasks() {
      return this.tasks.slice().sort((a, b) => {
        if (a.next_due_date !== b.next_due_date) {
          return a.next_due_date < b.next_due_date ? -1 : 1;
        }
        return a.id - b.id;
      });
    },

    todayLabel() {
      return this.now.toLocaleDateString(undefined, {
        weekday: "long",
        month: "long",
        day: "numeric",
      });
    },

    clockLabel() {
      return this.now.toLocaleTimeString(undefined, {
        hour: "numeric",
        minute: "2-digit",
      });
    },

    memberFilterLabel() {
      const hidden = this.hiddenMemberIds.length;
      if (!hidden) return "Filter";
      return hidden === 1 ? "Filter · 1 hidden" : `Filter · ${hidden} hidden`;
    },

    choreFilterLabel() {
      if (this.choreFilter === "all") return "All";
      if (this.choreFilter === "anyone") return "Anyone";
      const member = this.members.find((item) => item.id === this.choreFilter);
      return member ? member.name : "All";
    },

    setChoreFilter(value) {
      this.choreFilter = value;
      this.showChoreFilter = false;
    },

    init() {
      this.$watch("tab", (value) => {
        if (value !== "calendar") return;
        this.$nextTick(() => {
          this.ensureCalendar();
          if (this.calendar) this.calendar.updateSize();
        });
      });
      this.$nextTick(() => {
        this.setupKeyboard();
        this.$nextTick(() => this.ensureCalendar());
      });
      this.clockTimer = setInterval(() => {
        this.now = new Date();
      }, 1000);
      this.pollVersion();
      this.versionTimer = setInterval(() => this.pollVersion(), 60000);
      this.pollSync();
      this.syncTimer = setInterval(() => this.pollSync(), 15000);
      this.readLaunchParams();
      this.refreshAll();
    },

    destroy() {
      if (this.clockTimer) clearInterval(this.clockTimer);
      if (this.versionTimer) clearInterval(this.versionTimer);
      if (this.syncTimer) clearInterval(this.syncTimer);
      if (this.onFocusIn) document.removeEventListener("focusin", this.onFocusIn);
      if (this.onPointerDown) {
        document.removeEventListener("pointerdown", this.onPointerDown, true);
      }
    },

    async pollVersion() {
      try {
        const data = await request("/api/version", { cache: "no-store" });
        if (!data || !data.started_at) return;
        if (!this.serverStartedAt) {
          this.serverStartedAt = data.started_at;
          return;
        }
        if (data.started_at !== this.serverStartedAt) window.location.reload();
      } catch (_error) {
        /* Keep the kiosk up if a poll misses. */
      }
    },

    readLaunchParams() {
      const params = new URLSearchParams(window.location.search);
      if (params.get("settings") === "1") this.showSettings = true;
      if (params.get("auth_error")) this.settingsError = params.get("auth_error");
      if (params.get("linked")) {
        this.settingsMessage = "Google account linked. Run Sync now to pull events.";
      }
      if ([...params.keys()].length) {
        window.history.replaceState({}, "", window.location.pathname);
      }
    },

    syncLabel() {
      const status = this.syncStatus;
      if (!status.loaded) return "Checking sync…";
      if (status.syncing) return "Syncing…";
      if (this.calendarOffline) return "Offline";
      if (!status.linked_count) return "Calendar not linked";
      if (!status.last_synced_at) return "Linked";
      return `Synced ${this.formatSyncTime(status.last_synced_at)}`;
    },

    lastSyncLabel() {
      if (!this.syncStatus.last_synced_at) return "Has not synced yet";
      return `Last sync ${this.formatSyncTime(this.syncStatus.last_synced_at)}`;
    },

    formatSyncTime(iso) {
      const parsed = new Date(iso);
      if (Number.isNaN(parsed.getTime())) return iso;
      return parsed.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      });
    },

    oauthRedirectLabel() {
      return `${window.location.origin}/oauth2callback`;
    },

    async pollSync() {
      try {
        const status = await request("/api/sync/status", { cache: "no-store" });
        const previous = this.syncStatus.last_synced_at;
        this.syncStatus = { ...status, loaded: true };
        if (this.calendar && status.last_synced_at && status.last_synced_at !== previous) {
          this.calendar.refetchEvents();
        }
      } catch (_error) {
        /* The next poll can refresh the badge if this one misses. */
      }
    },

    openSettings() {
      this.settingsError = "";
      this.settingsMessage = "";
      this.unlinkArmedId = null;
      this.showSettings = true;
      this.pollSync();
    },

    closeSettings() {
      this.showSettings = false;
      this.unlinkArmedId = null;
      this.closeKeyboard(true);
    },

    async saveCalendarId(member) {
      const value = (member.google_calendar_id || "").trim() || "primary";
      this.setBusy(`cal-${member.id}`, true);
      this.settingsError = "";
      try {
        const updated = await request(`/api/family-members/${member.id}/calendar`, {
          method: "PUT",
          body: { google_calendar_id: value },
        });
        const index = this.members.findIndex((item) => item.id === member.id);
        if (index >= 0) this.members.splice(index, 1, updated);
        this.settingsMessage = `${updated.name} will sync ${updated.google_calendar_id}.`;
        return true;
      } catch (error) {
        this.settingsError = error.message;
        return false;
      } finally {
        this.setBusy(`cal-${member.id}`, false);
      }
    },

    async linkAccount(member) {
      this.settingsError = "";
      if (this.syncStatus.loaded && !this.syncStatus.credentials_present) {
        this.settingsError = "Add credentials.json next to main.py before linking a Google account.";
        return;
      }
      const typed = (member.google_calendar_id || "").trim();
      if (typed) {
        const saved = await this.saveCalendarId(member);
        if (!saved) return;
      }
      window.location.assign(`/api/auth/google/${member.id}`);
    },

    async unlinkAccount(member) {
      if (this.unlinkArmedId !== member.id) {
        this.unlinkArmedId = member.id;
        return;
      }
      this.setBusy(`unlink-${member.id}`, true);
      this.settingsError = "";
      try {
        await request(`/api/auth/google/${member.id}`, { method: "DELETE" });
        this.unlinkArmedId = null;
        this.settingsMessage = `${member.name} is unlinked.`;
        await this.refreshAll();
        await this.pollSync();
      } catch (error) {
        this.settingsError = error.message;
        this.unlinkArmedId = null;
      } finally {
        this.setBusy(`unlink-${member.id}`, false);
      }
    },

    async syncNow() {
      this.setBusy("sync", true);
      this.settingsError = "";
      try {
        const status = await request("/api/sync", { method: "POST" });
        this.syncStatus = { ...status, loaded: true };
        this.settingsMessage = status.detail || "Sync finished.";
        if (this.calendar) this.calendar.refetchEvents();
      } catch (error) {
        this.settingsError = error.message;
      } finally {
        this.setBusy("sync", false);
      }
    },

    async refreshAll() {
      this.loading = true;
      try {
        const [members, chores, assets, tasks] = await Promise.all([
          request("/api/family-members"),
          request("/api/chores"),
          request("/api/assets"),
          request("/api/maintenance-tasks"),
        ]);
        this.members = members;
        this.chores = chores;
        this.assets = assets;
        this.tasks = tasks;
        this.error = "";
      } catch (error) {
        this.error = error.message || "Could not reach the server.";
      } finally {
        this.loading = false;
      }
    },

    setupKeyboard() {
      if (this.kb) return;
      const ctor = window.SimpleKeyboard && (window.SimpleKeyboard.default || window.SimpleKeyboard);
      if (!ctor) {
        this.error = "On-screen keyboard failed to load.";
        return;
      }
      this.kb = new ctor("#osk", {
        layout: KEYBOARD_LAYOUT,
        layoutName: "default",
        display: {
          "{bksp}": "⌫",
          "{shift}": "⇧",
          "{space}": "space",
          "{done}": "Done",
        },
        theme: "hg-theme-default",
        buttonTheme: [{ class: "hk-done", buttons: "{done}" }],
        preventMouseDownDefault: true,
        newLineOnEnter: true,
        onChange: (input) => this.applyToActiveField(input),
        onKeyPress: (button) => this.onKeyboardKeyPress(button),
      });
      const keys = document.getElementById("osk");
      if (keys) {
        keys.addEventListener("mousedown", (event) => event.preventDefault());
      }
      this.onFocusIn = (event) => this.handleFocusIn(event);
      this.onPointerDown = (event) => this.handlePointerDown(event);
      document.addEventListener("focusin", this.onFocusIn);
      document.addEventListener("pointerdown", this.onPointerDown, true);
    },

    isTextField(element) {
      if (!element || !element.tagName) return false;
      if (element.closest && element.closest("#keyboard-dock")) return false;
      if (element.tagName === "TEXTAREA") return !element.readOnly && !element.disabled;
      if (element.tagName !== "INPUT") return false;
      const type = (element.getAttribute("type") || "text").toLowerCase();
      const allowed = ["text", "search", "email", "url", "tel", "password", "number"];
      return allowed.includes(type) && !element.readOnly && !element.disabled;
    },

    isInsideKeyboard(element) {
      const dock = document.getElementById("keyboard-dock");
      return !!(dock && element && dock.contains(element));
    },

    handleFocusIn(event) {
      const target = event.target;
      if (this.isTextField(target)) {
        this.openKeyboardFor(target);
        return;
      }
      if (this.isInsideKeyboard(target)) return;
      if (this.keyboardOpen) this.closeKeyboard(false);
    },

    handlePointerDown(event) {
      if (!this.keyboardOpen) return;
      const target = event.target;
      if (this.isInsideKeyboard(target) || this.isTextField(target)) return;
      this.closeKeyboard(false);
    },

    openKeyboardFor(element) {
      if (!this.kb) return;
      this.activeField = element;
      this.keyboardOpen = true;
      const layout = element.dataset.keyboard === "numeric" ? "numeric" : "default";
      this.kb.setOptions({
        layoutName: layout,
        maxLength: element.maxLength > 0 ? element.maxLength : 500,
      });
      this.kb.setInput(element.value || "");
      this.$nextTick(() => {
        if (typeof element.scrollIntoView === "function") {
          element.scrollIntoView({ block: "center", behavior: "smooth" });
        }
      });
    },

    closeKeyboard(blur) {
      this.keyboardOpen = false;
      const field = this.activeField;
      if (blur && field && document.activeElement === field) field.blur();
      if (blur) this.activeField = null;
    },

    applyToActiveField(value) {
      const element = this.activeField;
      if (!element || !element.isConnected) return;
      const prototype =
        element.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(prototype, "value").set;
      setter.call(element, value);
      element.dispatchEvent(new Event("input", { bubbles: true }));
    },

    onKeyboardKeyPress(button) {
      if (!this.kb) return;
      if (button === "{shift}") {
        const next = this.kb.options.layoutName === "shift" ? "default" : "shift";
        this.kb.setOptions({ layoutName: next });
        return;
      }
      if (button === "{done}") {
        this.closeKeyboard(true);
        return;
      }
      if (this.kb.options.layoutName === "shift" && button.length === 1) {
        this.kb.setOptions({ layoutName: "default" });
      }
    },

    formatDate(iso) {
      if (!iso) return "Not recorded";
      const [year, month, day] = iso.split("-").map(Number);
      return new Date(year, month - 1, day).toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
      });
    },

    choreDueText(chore) {
      if (!chore.due_date) return "No due date";
      const today = localISODate();
      if (chore.due_date === today) return "Due today";
      return `Due ${this.formatDate(chore.due_date)}`;
    },

    isChoreOverdue(chore) {
      return !chore.completed && chore.due_date && chore.due_date < localISODate();
    },

    statusLabel(task) {
      if (task.status === "overdue") return "Overdue";
      if (task.status === "due_soon") return "Due soon";
      return "On track";
    },

    duePhrase(task) {
      const days = task.days_until_due;
      if (days < 0) {
        const count = Math.abs(days);
        return count === 1 ? "1 day overdue" : `${count} days overdue`;
      }
      if (days === 0) return "Due today";
      if (days === 1) return "Due tomorrow";
      return `Due in ${days} days`;
    },

    intervalLabel(task) {
      const unit = task.interval_value === 1 ? task.interval_unit.replace(/s$/, "") : task.interval_unit;
      return `Every ${task.interval_value} ${unit}`;
    },

    taskCount(asset) {
      return this.tasks.filter((task) => task.asset_id === asset.id).length;
    },

    isBusy(key) {
      return !!this.busy[key];
    },

    setBusy(key, value) {
      const next = { ...this.busy };
      if (value) next[key] = true;
      else delete next[key];
      this.busy = next;
    },

    openAddChore() {
      this.choreForm = blankChore();
      this.showChoreModal = true;
      this.$nextTick(() => this.$refs.choreTitle && this.$refs.choreTitle.focus());
    },

    openEditChore(chore) {
      this.choreForm = {
        id: chore.id,
        title: chore.title,
        notes: chore.notes || "",
        family_member_id: chore.family_member_id == null ? "" : String(chore.family_member_id),
        due_date: chore.due_date || "",
      };
      this.showChoreModal = true;
      this.$nextTick(() => this.$refs.choreTitle && this.$refs.choreTitle.focus());
    },

    closeChoreModal() {
      this.showChoreModal = false;
      this.closeKeyboard(true);
    },

    chorePayload() {
      return {
        title: this.choreForm.title.trim(),
        notes: this.choreForm.notes.trim(),
        family_member_id:
          this.choreForm.family_member_id === "" ? null : Number(this.choreForm.family_member_id),
        due_date: this.choreForm.due_date || null,
      };
    },

    async saveChore() {
      if (!this.choreForm.title.trim()) {
        this.error = "A chore needs a title.";
        return;
      }
      this.setBusy("chore-save", true);
      try {
        if (this.choreForm.id) {
          await request(`/api/chores/${this.choreForm.id}`, {
            method: "PUT",
            body: this.chorePayload(),
          });
        } else {
          await request("/api/chores", { method: "POST", body: this.chorePayload() });
        }
        this.closeChoreModal();
        await this.refreshAll();
      } catch (error) {
        this.error = error.message;
      } finally {
        this.setBusy("chore-save", false);
      }
    },

    async toggleChore(chore) {
      const key = `chore-${chore.id}`;
      if (this.isBusy(key)) return;
      const previous = chore.completed;
      chore.completed = !chore.completed;
      this.setBusy(key, true);
      try {
        const updated = await request(`/api/chores/${chore.id}/toggle`, { method: "POST" });
        const index = this.chores.findIndex((item) => item.id === chore.id);
        if (index >= 0) this.chores.splice(index, 1, updated);
      } catch (error) {
        chore.completed = previous;
        this.error = error.message;
      } finally {
        this.setBusy(key, false);
      }
    },

    openAddAsset() {
      this.assetForm = blankAsset();
      this.showAssetModal = true;
      this.$nextTick(() => this.$refs.assetName && this.$refs.assetName.focus());
    },

    openEditAsset(asset) {
      this.assetForm = {
        id: asset.id,
        name: asset.name,
        location: asset.location || "",
        notes: asset.notes || "",
      };
      this.showAssetModal = true;
      this.$nextTick(() => this.$refs.assetName && this.$refs.assetName.focus());
    },

    closeAssetModal() {
      this.showAssetModal = false;
      this.closeKeyboard(true);
    },

    assetPayload() {
      return {
        name: this.assetForm.name.trim(),
        location: this.assetForm.location.trim(),
        notes: this.assetForm.notes.trim(),
      };
    },

    async saveAsset() {
      if (!this.assetForm.name.trim()) {
        this.error = "An asset needs a name.";
        return;
      }
      this.setBusy("asset-save", true);
      try {
        if (this.assetForm.id) {
          await request(`/api/assets/${this.assetForm.id}`, {
            method: "PUT",
            body: this.assetPayload(),
          });
        } else {
          await request("/api/assets", { method: "POST", body: this.assetPayload() });
        }
        this.closeAssetModal();
        await this.refreshAll();
      } catch (error) {
        this.error = error.message;
      } finally {
        this.setBusy("asset-save", false);
      }
    },

    openAddTask() {
      const form = blankTask();
      if (this.assets.length) form.asset_id = String(this.assets[0].id);
      this.taskForm = form;
      this.showTaskModal = true;
      this.$nextTick(() => this.$refs.taskTitle && this.$refs.taskTitle.focus());
    },

    openEditTask(task) {
      this.taskForm = {
        id: task.id,
        asset_id: String(task.asset_id),
        title: task.title,
        notes: task.notes || "",
        interval_value: String(task.interval_value),
        interval_unit: task.interval_unit,
        last_done_date: task.last_done_date || "",
      };
      this.showTaskModal = true;
      this.$nextTick(() => this.$refs.taskTitle && this.$refs.taskTitle.focus());
    },

    closeTaskModal() {
      this.showTaskModal = false;
      this.closeKeyboard(true);
    },

    taskPayload() {
      return {
        asset_id: Number(this.taskForm.asset_id),
        title: this.taskForm.title.trim(),
        notes: this.taskForm.notes.trim(),
        interval_value: Number(this.taskForm.interval_value),
        interval_unit: this.taskForm.interval_unit,
        last_done_date: this.taskForm.last_done_date || null,
      };
    },

    async saveTask() {
      if (!this.assets.length) {
        this.error = "Add an asset before adding a maintenance task.";
        return;
      }
      if (!this.taskForm.title.trim()) {
        this.error = "A maintenance task needs a title.";
        return;
      }
      const interval = Number(this.taskForm.interval_value);
      if (!Number.isInteger(interval) || interval < 1) {
        this.error = "Interval needs a whole number of 1 or more.";
        return;
      }
      this.setBusy("task-save", true);
      try {
        if (this.taskForm.id) {
          await request(`/api/maintenance-tasks/${this.taskForm.id}`, {
            method: "PUT",
            body: this.taskPayload(),
          });
        } else {
          await request("/api/maintenance-tasks", { method: "POST", body: this.taskPayload() });
        }
        this.closeTaskModal();
        await this.refreshAll();
      } catch (error) {
        this.error = error.message;
      } finally {
        this.setBusy("task-save", false);
      }
    },

    async markTaskDone(task) {
      const key = `done-${task.id}`;
      if (this.isBusy(key)) return;
      this.setBusy(key, true);
      try {
        const updated = await request(`/api/maintenance-tasks/${task.id}/done`, { method: "POST" });
        const index = this.tasks.findIndex((item) => item.id === task.id);
        if (index >= 0) this.tasks.splice(index, 1, updated);
      } catch (error) {
        this.error = error.message;
      } finally {
        this.setBusy(key, false);
      }
    },

    askDelete(kind, item) {
      this.closeKeyboard(true);
      this.pendingDelete = {
        kind,
        id: item.id,
        name: item.name || item.title,
      };
      this.showDeleteModal = true;
    },

    closeDeleteModal() {
      this.showDeleteModal = false;
      this.pendingDelete = null;
    },

    minutesLabel(minutes) {
      return minutesLabel(minutes);
    },

    isMemberVisible(memberId) {
      return !this.hiddenMemberIds.includes(memberId);
    },

    toggleMemberFilter(memberId) {
      if (this.isMemberVisible(memberId)) {
        this.hiddenMemberIds = this.hiddenMemberIds.concat(memberId);
      } else {
        this.hiddenMemberIds = this.hiddenMemberIds.filter((id) => id !== memberId);
      }
      if (this.calendar) this.calendar.refetchEvents();
    },

    toCalendarEvent(row) {
      const color = row.color_hex || "#57534e";
      return {
        id: row.occurrence_id,
        title: row.title,
        start: row.start_time,
        end: row.end_time,
        allDay: !!row.all_day,
        backgroundColor: color,
        borderColor: color,
        textColor: textOnColor(color),
        extendedProps: row,
      };
    },

    ensureCalendar() {
      if (this.calendar || !this.$refs.calendarHost) return;
      const Calendar = window.FullCalendar && window.FullCalendar.Calendar;
      if (!Calendar) {
        this.error = "Calendar failed to load.";
        return;
      }
      this.calendar = new Calendar(this.$refs.calendarHost, {
        initialView: "timeGridWeek",
        headerToolbar: false,
        height: "100%",
        firstDay: 0,
        allDaySlot: true,
        nowIndicator: true,
        slotDuration: "00:30:00",
        snapDuration: "00:15:00",
        scrollTime: "07:00:00",
        eventMinHeight: 48,
        expandRows: false,
        dayHeaderFormat: { weekday: "short", month: "numeric", day: "numeric" },
        eventTimeFormat: { hour: "numeric", minute: "2-digit", meridiem: "short" },
        views: {
          timeGridThreeDay: {
            type: "timeGrid",
            duration: { days: 3 },
          },
        },
        events: (info, success, failure) => {
          const params = new URLSearchParams({
            start: localDateTimeISO(info.start),
            end: localDateTimeISO(info.end),
          });
          request(`/api/events?${params.toString()}`)
            .then((rows) => {
              const visible = rows.filter(
                (row) => row.member_id == null || this.isMemberVisible(row.member_id),
              );
              this.error = "";
              success(visible.map((row) => this.toCalendarEvent(row)));
            })
            .catch((error) => {
              this.error = error.message || "Could not load events.";
              failure(error);
            });
        },
        dateClick: (info) => {
          if (this.showEventModal) return;
          this.openEventModal(null, info.date, info.allDay);
        },
        eventClick: (info) => {
          info.jsEvent.preventDefault();
          this.openEventModal(info.event.extendedProps);
        },
        datesSet: (info) => {
          this.calendarView = info.view.type;
          this.calendarLabel = info.view.title;
        },
      });
      this.calendar.render();
    },

    setCalendarView(view) {
      this.calendarView = view;
      if (this.calendar) this.calendar.changeView(view);
    },

    moveCalendar(action) {
      if (this.calendar && typeof this.calendar[action] === "function") {
        this.calendar[action]();
      }
    },

    repeatLabel(rrule) {
      if (!rrule) return "";
      let when = "on a schedule";
      if (/FREQ=DAILY/i.test(rrule)) when = "every day";
      else if (/FREQ=WEEKLY/i.test(rrule)) when = "every week";
      else if (/FREQ=MONTHLY/i.test(rrule)) when = "every month";
      else if (/FREQ=YEARLY/i.test(rrule)) when = "every year";
      return `Repeats ${when}. Saving or deleting changes the whole series.`;
    },

    openEventModal(existing, date, allDay) {
      this.eventDeleteArmed = false;
      this.eventError = "";
      this.eventForm = existing ? formFromEvent(existing) : blankEvent(date, allDay);
      this.showEventModal = true;
      this.$nextTick(() => this.$refs.eventTitle && this.$refs.eventTitle.focus());
    },

    openNewEvent() {
      const base = this.calendar ? new Date(this.calendar.getDate()) : new Date();
      base.setHours(9, 0, 0, 0);
      this.openEventModal(null, base, false);
    },

    closeEventModal() {
      this.showEventModal = false;
      this.eventDeleteArmed = false;
      this.eventError = "";
      this.closeKeyboard(true);
    },

    toggleAllDay() {
      this.eventDeleteArmed = false;
      this.eventForm.all_day = !this.eventForm.all_day;
      if (!this.eventForm.all_day && this.eventForm.end_date === this.eventForm.start_date) {
        const end = addMinutes(this.eventForm.start_date, this.eventForm.start_minutes, 60);
        this.eventForm.end_date = end.date;
        this.eventForm.end_minutes = end.minutes;
      }
    },

    currentEventDuration() {
      if (this.eventForm.all_day) {
        return Math.max(1, daysBetween(this.eventForm.start_date, this.eventForm.end_date) + 1);
      }
      return Math.max(
        15,
        spanMinutes(
          this.eventForm.start_date,
          this.eventForm.start_minutes,
          this.eventForm.end_date,
          this.eventForm.end_minutes,
        ),
      );
    },

    shiftEventDay(which, delta) {
      this.eventDeleteArmed = false;
      const form = this.eventForm;
      if (which === "start") {
        const next = addDaysISO(form.start_date, delta);
        if (form.all_day) {
          const length = Math.max(0, daysBetween(form.start_date, form.end_date));
          form.start_date = next;
          form.end_date = addDaysISO(next, length);
        } else {
          const duration = this.currentEventDuration();
          form.start_date = next;
          const end = addMinutes(next, form.start_minutes, duration);
          form.end_date = end.date;
          form.end_minutes = end.minutes;
        }
        return;
      }
      const next = addDaysISO(form.end_date, delta);
      if (next < form.start_date) return;
      form.end_date = next;
    },

    onEventDateChange(which) {
      this.eventDeleteArmed = false;
      const form = this.eventForm;
      if (!form.start_date) form.start_date = localISODate();
      if (!form.end_date || form.end_date < form.start_date) form.end_date = form.start_date;
      if (which === "start" && !form.all_day) {
        const duration = 60;
        const end = addMinutes(form.start_date, form.start_minutes, duration);
        if (spanMinutes(form.start_date, form.start_minutes, form.end_date, form.end_minutes) <= 0) {
          form.end_date = end.date;
          form.end_minutes = end.minutes;
        }
      }
    },

    shiftEventTime(which, delta) {
      this.eventDeleteArmed = false;
      const form = this.eventForm;
      if (which === "start") {
        const duration = this.currentEventDuration();
        let minutes = form.start_minutes + delta;
        let date = form.start_date;
        while (minutes >= 1440) {
          minutes -= 1440;
          date = addDaysISO(date, 1);
        }
        while (minutes < 0) {
          minutes += 1440;
          date = addDaysISO(date, -1);
        }
        form.start_date = date;
        form.start_minutes = minutes;
        const end = addMinutes(date, minutes, duration);
        form.end_date = end.date;
        form.end_minutes = end.minutes;
        return;
      }
      let minutes = form.end_minutes + delta;
      let date = form.end_date;
      while (minutes >= 1440) {
        minutes -= 1440;
        date = addDaysISO(date, 1);
      }
      while (minutes < 0) {
        minutes += 1440;
        date = addDaysISO(date, -1);
      }
      if (spanMinutes(form.start_date, form.start_minutes, date, minutes) < 15) {
        const bumped = addMinutes(form.start_date, form.start_minutes, 15);
        date = bumped.date;
        minutes = bumped.minutes;
      }
      form.end_date = date;
      form.end_minutes = minutes;
    },

    async saveEvent() {
      this.eventDeleteArmed = false;
      if (this.calendarOffline) {
        this.eventError = "Offline – Calendar edits paused";
        return;
      }
      if (!this.eventForm.title.trim()) {
        this.eventError = "An event needs a title.";
        return;
      }
      if (!this.eventForm.all_day) {
        const duration = spanMinutes(
          this.eventForm.start_date,
          this.eventForm.start_minutes,
          this.eventForm.end_date,
          this.eventForm.end_minutes,
        );
        if (duration < 15) {
          this.eventError = "End time must be after the start time.";
          return;
        }
      } else if (this.eventForm.end_date < this.eventForm.start_date) {
        this.eventError = "End date must be on or after the start date.";
        return;
      }
      const payload = eventPayloadFromForm(this.eventForm);
      this.setBusy("event-save", true);
      try {
        if (this.eventForm.id) {
          await request(`/api/events/${this.eventForm.id}`, { method: "PUT", body: payload });
        } else {
          await request("/api/events", { method: "POST", body: payload });
        }
        this.closeEventModal();
        if (this.calendar) this.calendar.refetchEvents();
      } catch (error) {
        this.eventError = error.message;
      } finally {
        this.setBusy("event-save", false);
      }
    },

    async deleteEventTap() {
      if (!this.eventForm.id) return;
      if (this.calendarOffline) {
        this.eventError = "Offline – Calendar edits paused";
        return;
      }
      if (!this.eventDeleteArmed) {
        this.eventDeleteArmed = true;
        return;
      }
      this.setBusy("event-delete", true);
      try {
        await request(`/api/events/${this.eventForm.id}`, { method: "DELETE" });
        this.closeEventModal();
        if (this.calendar) this.calendar.refetchEvents();
      } catch (error) {
        this.eventError = error.message;
        this.eventDeleteArmed = false;
      } finally {
        this.setBusy("event-delete", false);
      }
    },

    async confirmDelete() {
      const pending = this.pendingDelete;
      if (!pending) return;
      const paths = {
        chore: `/api/chores/${pending.id}`,
        asset: `/api/assets/${pending.id}`,
        task: `/api/maintenance-tasks/${pending.id}`,
      };
      this.setBusy("delete", true);
      try {
        await request(paths[pending.kind], { method: "DELETE" });
        this.closeDeleteModal();
        await this.refreshAll();
      } catch (error) {
        this.error = error.message;
      } finally {
        this.setBusy("delete", false);
      }
    },
  }));
});
