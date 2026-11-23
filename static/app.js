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
    showDeleteModal: false,
    pendingDelete: null,
    choreForm: blankChore(),
    assetForm: blankAsset(),
    taskForm: blankTask(),

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
      return new Date().toLocaleDateString(undefined, {
        weekday: "long",
        month: "long",
        day: "numeric",
      });
    },

    init() {
      this.$nextTick(() => this.setupKeyboard());
      this.pollVersion();
      this.versionTimer = setInterval(() => this.pollVersion(), 60000);
      this.refreshAll();
    },

    destroy() {
      if (this.versionTimer) clearInterval(this.versionTimer);
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
