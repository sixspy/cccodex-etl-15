const { createApp } = Vue;

createApp({
  data() {
    return {
      me: "",
      role: "",
      items: [],
      current: null,
      overview: {},
      policyDoc: { policy: null, choices: [], locked_today: false },
      keys: [],
      rollbacks: [],
      policyForm: { policy: "skip_duplicate", description: "" },
      idemKey: "",
      rollbackReason: "",
      busy: false,
      msg: "",
      msgKind: "muted",
    };
  },
  computed: {
    policy() {
      return this.policyDoc.policy || {};
    },
    policyChoices() {
      return this.policyDoc.choices || [];
    },
    policyLocked() {
      return !!this.policyDoc.locked_today;
    },
    policyOn() {
      return !!this.overview.policy_name;
    },
    ledgerRows() {
      const l = this.current && this.current.ledger;
      if (!l) return [];
      return [
        ...(l.batches || []),
        ...(l.claims || []),
        ...(l.rollbacks || []),
        ...(l.events || []),
      ];
    },
  },
  async mounted() {
    const me = await fetch("/api/me");
    if (!me.ok) {
      location.href = "/login";
      return;
    }
    const info = await me.json();
    this.me = info.user;
    this.role = info.role;
    await this.refreshAll();
  },
  methods: {
    tagClass(status) {
      if (status === "已落库") return "ok";
      if (status === "已抽取待加载") return "warn";
      return "";
    },
    flash(text, kind = "muted") {
      this.msg = text;
      this.msgKind = kind;
    },
    async refreshAll() {
      await Promise.all([this.refresh(), this.refreshOverview(), this.refreshPolicy(), this.refreshKeys(), this.refreshRollbacks()]);
    },
    async refresh() {
      const res = await fetch("/api/pipelines");
      if (!res.ok) return;
      this.items = (await res.json()).items || [];
    },
    async refreshOverview() {
      const res = await fetch("/api/overview");
      if (res.ok) this.overview = await res.json();
    },
    async refreshPolicy() {
      const res = await fetch("/api/idempotency/policy");
      if (!res.ok) return;
      this.policyDoc = await res.json();
      const p = this.policyDoc.policy;
      if (p) {
        this.policyForm = { policy: p.policy, description: p.description };
      }
    },
    async refreshKeys() {
      const res = await fetch("/api/idempotency/keys");
      if (res.ok) this.keys = (await res.json()).items || [];
    },
    async refreshRollbacks() {
      const res = await fetch("/api/rollbacks");
      if (res.ok) this.rollbacks = (await res.json()).items || [];
    },
    async open(id) {
      const res = await fetch(`/api/pipelines/${id}`);
      if (!res.ok) return;
      this.current = await res.json();
      this.msg = "";
      this.idemKey = "";
      this.rollbackReason = "";
    },
    async reloadCurrent() {
      if (this.current) await this.open(this.current.id);
    },
    async savePolicy() {
      this.busy = true;
      try {
        const res = await fetch("/api/idempotency/policy", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this.policyForm),
        });
        const data = await res.json();
        if (!res.ok) {
          this.flash(data.error || "策略保存失败", "err");
          return;
        }
        this.flash(`已锁定当日口径：${data.label}`, "ok");
        await this.refreshAll();
      } finally {
        this.busy = false;
      }
    },
    async extract() {
      if (!this.current) return;
      this.busy = true;
      this.flash("");
      try {
        const res = await fetch(`/api/pipelines/${this.current.id}/extract`, {
          method: "POST",
        });
        const data = await res.json();
        if (!res.ok) {
          this.flash(data.error || "抽取失败", "err");
          return;
        }
        this.current = { ...data, ledger: this.current.ledger };
        this.flash("抽取完成，可继续落库", "ok");
        await this.refresh();
      } finally {
        this.busy = false;
      }
    },
    async load() {
      if (!this.current) return;
      this.busy = true;
      this.flash("");
      try {
        const res = await fetch(`/api/pipelines/${this.current.id}/load`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            idem_key: this.idemKey,
            policy: this.overview.policy_name || undefined,
            policy_description: this.overview.policy_description || undefined,
          }),
        });
        const data = await res.json();
        if (!res.ok) {
          this.flash(data.error || "落库失败", "err");
        } else if (data.skipped) {
          this.flash(`${data.message}（累计跳过 ${data.skipped_count}）`, "warn");
        } else {
          this.flash(`落库完成，批次 #${data.batch.id}`, "ok");
        }
        await this.reloadCurrent();
        await this.refreshAll();
      } finally {
        this.busy = false;
      }
    },
    async rollback() {
      if (!this.current) return;
      this.busy = true;
      this.flash("");
      try {
        const res = await fetch(`/api/pipelines/${this.current.id}/rollback`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: this.rollbackReason }),
        });
        const data = await res.json();
        if (!res.ok) {
          this.flash(data.error || "回滚失败", "err");
        } else {
          this.flash(data.message, "ok");
        }
        await this.reloadCurrent();
        await this.refreshAll();
      } finally {
        this.busy = false;
      }
    },
    async logout() {
      await fetch("/api/logout", { method: "POST" });
      location.href = "/login";
    },
  },
}).mount("#app");
