const { createApp, nextTick } = Vue;

createApp({
  data() {
    return {
      query: "",
      maxRetries: 2,
      loading: false,
      statusText: "",
      error: null,
      activeTab: "summary",
      
      stages: {
        sql: { key: "sql", label: "Generated SQL & Audit", status: "idle", error: null, timeMs: null },
        data: { key: "data", label: "Data Preview (10 Rows)", status: "idle", error: null, timeMs: null },
        chart: { key: "chart", label: "Interactive Chart", status: "idle", error: null, timeMs: null },
        summary: { key: "summary", label: "Executive Summary", status: "idle", error: null, timeMs: null },
        reports: { key: "reports", label: "Export Reports", status: "idle", error: null, timeMs: null }
      },

      result: {
        guardrail_passed: true,
        guardrail_rejection_reason: "",
        route_decision: "TIER_1_SLM",
        route_confidence: 1.0,
        model_used: "N/A",
        generated_sql: "",
        final_response: "",
        summary: "",
        key_findings: [],
        anomalies: [],
        total_cost_usd: 0.0,
        total_latency_ms: 0,
        retry_count: 0,
        error_history: [],
        ambiguity_flag: false,
        interpretation_note: "",
        data: {
          columns: [],
          rows: [],
          total_rows: 0,
          truncated: false
        },
        reports: {}
      },

      chartOptions: [],
      selectedChartIndex: 0,

      pdfPreviewUrl: null,
      pdfLoading: false,

      tablesCatalog: [],
      hoveredTable: null,
      popoverStyle: { top: "0px", left: "0px" },
      hoverTimeout: null,

      health: {
        status: "checking",
        table_count: 0,
        tables: []
      },

      sampleQueries: [
        "Top 10 product categories by total sales revenue",
        "Monthly order trend and revenue for 2017 to 2018",
        "Show distribution of payment types as a pie chart",
        "Average delivery delay in days grouped by customer state"
      ],

      telemetryLogs: [],
      telemetryStats: {
        total_queries: 0,
        success_count: 0,
        failed_count: 0,
        blocked_count: 0,
        success_rate_pct: 100.0,
        avg_latency_ms: 0.0,
        total_cost_usd: 0.0,
        tier1_count: 0,
        tier2_count: 0,
        last_run_status: "IDLE"
      },
      telemetryFilter: "ALL",
      selectedLog: null,

      authToken: localStorage.getItem("agent_auth_token") || "",
      currentUser: null,
      authChecking: true,
      authMode: "login",
      authForm: {
        loginId: "",
        username: "",
        email: "",
        name: "",
        displayName: "",
        password: ""
      },
      authError: null,
      authLoading: false,
      sessionId: localStorage.getItem("agent_session_id") || ("sess_" + Math.random().toString(36).substring(2, 9)),
      userHistory: [],
      historyLoading: false,

      datasetMode: localStorage.getItem("agent_dataset_mode") || "demo",
      userTables: [],
      showUploadModal: false,
      uploadFileObj: null,
      uploadCustomTableName: "",
      uploadLoading: false,
      uploadError: null,
      uploadSuccessMsg: null
    };
  },

  mounted() {
    this.checkAuth();
    this.checkHealth();
    this.fetchTableCatalog();
    this.fetchTelemetryLogs();
  },

  computed: {
    renderedSummary() {
      const raw = this.result && this.result.final_response ? this.result.final_response : "";
      if (!raw) return "";

      if (typeof marked !== "undefined" && typeof marked.parse === "function") {
        try {
          return marked.parse(raw);
        } catch (e) {
          console.warn("Marked.js parse error:", e);
        }
      }

      return this.formatMarkdownFallback(raw);
    }
  },

  methods: {
    formatMarkdownFallback(text) {
      if (!text) return "";
      let html = text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");

      html = html.replace(/^### (.*$)/gim, '<h5 class="fw-bold mt-3 mb-2 text-primary">$1</h5>');
      html = html.replace(/^## (.*$)/gim, '<h4 class="fw-bold mt-3 mb-2 text-primary">$1</h4>');
      html = html.replace(/^# (.*$)/gim, '<h3 class="fw-bold mt-3 mb-2 text-primary">$1</h3>');

      html = html.replace(/\*\*(.*?)\*\*/g, '<strong class="text-dark">$1</strong>');
      html = html.replace(/\*(.*?)\*/g, '<em>$1</em>');

      html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

      html = html.replace(/^\s*[-•]\s+(.*$)/gim, '<li class="mb-1">$1</li>');
      html = html.replace(/(<li.*<\/li>)/gms, '<ul class="ps-3 mb-3">$1</ul>');

      html = html.split("\n\n").map(p => {
        if (p.startsWith("<h") || p.startsWith("<ul")) return p;
        return `<p class="mb-2">${p.replace(/\n/g, "<br>")}</p>`;
      }).join("");

      return html;
    },

    async fetchTableCatalog() {
      try {
        const headers = {};
        if (this.authToken) headers["Authorization"] = "Bearer " + this.authToken;
        const res = await fetch(`/api/tables/preview?dataset_mode=${this.datasetMode}`, { headers });
        const contentType = res.headers.get("content-type") || "";
        if (res.ok && contentType.includes("application/json")) {
          const data = await res.json();
          this.tablesCatalog = data.tables || [];
        }
      } catch (err) {
        console.warn("Failed fetching table catalog:", err);
      }
    },

    showTablePreview(table, event) {
      if (this.hoverTimeout) {
        clearTimeout(this.hoverTimeout);
        this.hoverTimeout = null;
      }

      const rect = event.currentTarget.getBoundingClientRect();
      const popoverWidth = 580;
      let left = rect.left;
      if (left + popoverWidth > window.innerWidth - 20) {
        left = Math.max(10, window.innerWidth - popoverWidth - 20);
      }
      let top = rect.bottom + 8;
      if (top + 340 > window.innerHeight) {
        top = Math.max(10, rect.top - 350);
      }

      this.popoverStyle = {
        top: `${Math.round(top)}px`,
        left: `${Math.round(left)}px`
      };
      this.hoveredTable = table;
    },

    hideTablePreview() {
      this.hoverTimeout = setTimeout(() => {
        this.hoveredTable = null;
      }, 180);
    },

    keepTablePreview() {
      if (this.hoverTimeout) {
        clearTimeout(this.hoverTimeout);
        this.hoverTimeout = null;
      }
    },

    insertTableQuery(tableName) {
      if (this.query.trim()) {
        this.query += ` using table ${tableName}`;
      } else {
        this.query = `Show top 10 records from ${tableName}`;
      }
      this.hoveredTable = null;
    },

    async checkHealth() {
      try {
        const res = await fetch("/api/health");
        const data = await res.json();
        this.health = data;
      } catch (err) {
        this.health = {
          status: "offline",
          table_count: 0,
          tables: []
        };
      }
    },

    setSample(sample) {
      this.query = sample;
    },

    setTab(tabName) {
      this.activeTab = tabName;
      if (tabName === "chart") {
        nextTick(() => {
          this.renderActiveChart();
        });
      }
    },

    selectChartOption(index) {
      if (index >= 0 && index < this.chartOptions.length) {
        this.selectedChartIndex = index;
        nextTick(() => {
          this.renderActiveChart();
        });
      }
    },

    resetStages() {
      const keys = ["sql", "data", "chart", "summary", "reports"];
      keys.forEach((k) => {
        this.stages[k].status = "pending";
        this.stages[k].error = null;
        this.stages[k].timeMs = null;
      });
      this.result = {
        guardrail_passed: true,
        guardrail_rejection_reason: "",
        route_decision: "TIER_1_SLM",
        route_confidence: 1.0,
        model_used: "N/A",
        generated_sql: "",
        final_response: "",
        summary: "",
        key_findings: [],
        anomalies: [],
        total_cost_usd: 0.0,
        total_latency_ms: 0,
        retry_count: 0,
        error_history: [],
        ambiguity_flag: false,
        interpretation_note: "",
        data: {
          columns: [],
          rows: [],
          total_rows: 0,
          truncated: false
        },
        reports: {}
      };
      this.chartOptions = [];
      this.selectedChartIndex = 0;
      this.pdfPreviewUrl = null;
      this.pdfLoading = true;
    },

    async submitQuery() {
      const trimmed = this.query.trim();
      if (!trimmed) {
        this.error = "Please enter a question or query.";
        return;
      }

      this.loading = true;
      this.error = null;
      this.statusText = "Initializing pipeline...";
      this.resetStages();

      const startTime = performance.now();

      try {
        const reqHeaders = { "Content-Type": "application/json" };
        if (this.authToken) {
          reqHeaders["Authorization"] = "Bearer " + this.authToken;
        }

        const response = await fetch("/api/query/stream", {
          method: "POST",
          headers: reqHeaders,
          body: JSON.stringify({
            query: trimmed,
            max_retries: parseInt(this.maxRetries) || 2,
            session_id: this.sessionId,
            dataset_mode: this.datasetMode
          })
        });

        if (!response.ok) {
          throw new Error(`HTTP error: ${response.status} ${response.statusText}`);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            const trimmedLine = line.trim();
            if (trimmedLine.startsWith("data:")) {
              try {
                const payload = JSON.parse(trimmedLine.slice(5).trim());
                this.handleStreamEvent(payload, startTime);
              } catch (parseErr) {
                console.warn("SSE JSON parse error:", parseErr, trimmedLine);
              }
            }
          }
        }

      } catch (err) {
        console.error("Stream failed, falling back to standard execution:", err);
        await this.fallbackQuery(trimmed);
      } finally {
        this.loading = false;
        this.statusText = "";
      }
    },

    handleStreamEvent(data, startTime) {
      const now = performance.now();
      const elapsed = Math.round(now - startTime);

      if (data.event === "status") {
        this.statusText = data.message || "Processing...";
      }

      else if (data.event === "stage_update") {
        const stage = data.stage;
        if (this.stages[stage]) {
          this.stages[stage].status = data.status || "done";
          this.stages[stage].timeMs = elapsed;
          if (data.error) {
            this.stages[stage].error = data.error;
          }
        }

        if (stage === "sql" && data.data) {
          this.result.generated_sql = data.data.generated_sql || "";
          this.result.model_used = data.data.model_used || "N/A";
          this.result.route_decision = data.data.route_decision || "TIER_1_SLM";
          this.result.route_confidence = data.data.route_confidence || 1.0;
          this.result.error_history = data.data.error_history || [];
        }

        else if (stage === "data" && data.data) {
          this.result.data.columns = data.data.columns || [];
          this.result.data.rows = data.data.rows || [];
          this.result.data.total_rows = data.data.total_rows || (data.data.rows ? data.data.rows.length : 0);
          this.result.data.truncated = data.data.truncated || false;

          if (this.activeTab === "summary" && this.stages.summary.status === "pending") {
            this.activeTab = "data";
          }
        }

        else if (stage === "chart" && data.data) {
          this.chartOptions = data.data.chart_options || [];
          this.selectedChartIndex = 0;
          
          if (this.chartOptions.length > 0) {
            this.activeTab = "chart";
            nextTick(() => {
              this.renderActiveChart();
            });
          }
        }

        else if (stage === "summary" && data.data) {
          this.result.summary = data.data.summary || "";
          this.result.key_findings = data.data.key_findings || [];
          this.result.anomalies = data.data.anomalies || [];
          this.result.interpretation_note = data.data.interpretation_note || "";
          if (data.data.summary) {
            let compiled = data.data.summary;
            if (this.result.key_findings.length) {
              compiled += "\n\nKey Findings:\n" + this.result.key_findings.map(f => "  - " + f).join("\n");
            }
            if (this.result.anomalies.length) {
              compiled += "\n\nAnomalies:\n" + this.result.anomalies.map(a => "  - " + a).join("\n");
            }
            this.result.final_response = compiled;
          }
        }

        else if (stage === "reports" && data.data) {
          this.result.reports = data.data.reports || {};
          this.pdfLoading = false;
          if (this.result.reports.pdf_inline) {
            this.pdfPreviewUrl = this.result.reports.pdf_inline;
          } else if (this.result.reports.pdf) {
            this.pdfPreviewUrl = this.result.reports.pdf;
          }
        }
      }

      else if (data.event === "final_response_ready") {
        if (data.final_response) {
          this.result.final_response = data.final_response;
        }
      }

      else if (data.event === "guardrail_rejected") {
        this.result.guardrail_passed = false;
        this.result.guardrail_rejection_reason = data.reason || "Security policy violation";
        Object.keys(this.stages).forEach((k) => {
          if (this.stages[k].status === "pending") {
            this.stages[k].status = "failed";
          }
        });
        this.fetchTelemetryLogs();
      }

      else if (data.event === "complete") {
        this.result.total_latency_ms = data.total_latency_ms || elapsed;
        this.result.total_cost_usd = data.total_cost_usd || 0.0;
        this.result.retry_count = data.retry_count || 0;
        this.result.model_used = data.model_used || this.result.model_used;
        this.loading = false;
        this.fetchTelemetryLogs();
      }

      else if (data.event === "error") {
        this.error = data.detail || "An error occurred during query processing.";
        this.fetchTelemetryLogs();
      }
    },

    async fallbackQuery(queryText) {
      try {
        const reqHeaders = { "Content-Type": "application/json" };
        if (this.authToken) {
          reqHeaders["Authorization"] = "Bearer " + this.authToken;
        }
        const response = await fetch("/api/query", {
          method: "POST",
          headers: reqHeaders,
          body: JSON.stringify({
            query: queryText,
            max_retries: parseInt(this.maxRetries) || 2,
            session_id: this.sessionId,
            dataset_mode: this.datasetMode
          })
        });
        if (!response.ok) {
          const errData = await response.json();
          throw new Error(errData.detail || "Fallback query failed.");
        }
        const data = await response.json();
        this.result = data;
        this.chartOptions = data.chart_options || [];
        this.selectedChartIndex = 0;
        
        Object.keys(this.stages).forEach((k) => {
          this.stages[k].status = "done";
        });

        if (data.reports && data.reports.pdf_inline) {
          this.pdfPreviewUrl = data.reports.pdf_inline;
        } else if (data.reports && data.reports.pdf) {
          this.pdfPreviewUrl = data.reports.pdf;
        }

        if (this.chartOptions.length > 0) {
          this.activeTab = "chart";
          nextTick(() => { this.renderActiveChart(); });
        } else {
          this.activeTab = "summary";
        }
        this.fetchTelemetryLogs();
        this.fetchUserHistory();
      } catch (err) {
        this.error = err.message || "Failed to communicate with agent service.";
        Object.keys(this.stages).forEach((k) => {
          this.stages[k].status = "failed";
        });
        this.fetchTelemetryLogs();
      }
    },

    renderActiveChart() {
      const container = document.getElementById("chart-container");
      if (!container) return;
      if (typeof Plotly === "undefined") {
        console.warn("Plotly is not loaded yet.");
        return;
      }

      let figure = null;
      if (this.chartOptions && this.chartOptions.length > this.selectedChartIndex) {
        figure = this.chartOptions[this.selectedChartIndex].plotly_figure;
      } else if (this.result && this.result.chart_figure) {
        figure = this.result.chart_figure;
      }

      if (!figure) return;

      const data = figure.data || [];
      const layout = figure.layout || {};

      layout.autosize = true;
      layout.margin = layout.margin || { l: 50, r: 40, t: 50, b: 50 };

      Plotly.newPlot(container, data, layout, {
        responsive: true,
        displayModeBar: true,
        displaylogo: false
      });
    },

    formatCurrency(val) {
      if (val === null || val === undefined || isNaN(val)) return "$0.0000";
      return "$" + Number(val).toFixed(4);
    },

    formatLatency(ms) {
      if (!ms || isNaN(ms)) return "0 ms";
      if (ms > 1000) {
        return (ms / 1000).toFixed(2) + " s";
      }
      return Math.round(ms) + " ms";
    },

    async fetchTelemetryLogs() {
      try {
        const queryParam = this.telemetryFilter !== "ALL" ? `?status=${this.telemetryFilter}` : "";
        const res = await fetch(`/api/logs${queryParam}`);
        if (res.ok) {
          const data = await res.json();
          this.telemetryStats = data.stats || this.telemetryStats;
          this.telemetryLogs = data.logs || [];
        }
      } catch (err) {
        console.warn("Failed fetching telemetry logs:", err);
      }
    },

    setTelemetryFilter(filterName) {
      this.telemetryFilter = filterName;
      this.fetchTelemetryLogs();
    },

    async clearTelemetryLogs() {
      if (!confirm("Are you sure you want to clear all telemetry and execution logs?")) return;
      try {
        const res = await fetch("/api/logs", { method: "DELETE" });
        if (res.ok) {
          this.selectedLog = null;
          await this.fetchTelemetryLogs();
        }
      } catch (err) {
        console.warn("Failed clearing logs:", err);
      }
    },

    showLogDetail(log) {
      this.selectedLog = log;
    },

    formatLogTime(isoStr) {
      if (!isoStr) return "";
      try {
        const d = new Date(isoStr);
        return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) +
               " " + d.toLocaleDateString([], { month: "short", day: "numeric" });
      } catch (e) {
        return isoStr;
      }
    },

    async checkAuth() {
      if (!this.authToken) {
        this.authChecking = false;
        return;
      }
      this.authChecking = true;
      try {
        const res = await fetch("/api/auth/me", {
          headers: { "Authorization": "Bearer " + this.authToken }
        });
        const contentType = res.headers.get("content-type") || "";
        if (res.ok && contentType.includes("application/json")) {
          const user = await res.json();
          this.currentUser = user;
          this.fetchUserHistory();
          this.fetchUserTables();
        } else {
          this.authToken = "";
          this.currentUser = null;
          localStorage.removeItem("agent_auth_token");
        }
      } catch (err) {
        console.warn("Auth check error:", err);
        this.authToken = "";
        this.currentUser = null;
        localStorage.removeItem("agent_auth_token");
      } finally {
        this.authChecking = false;
      }
    },

    async handleLogin() {
      this.authError = null;
      const ident = (this.authForm.loginId || this.authForm.email || this.authForm.username || "").trim();
      if (!ident || !this.authForm.password) {
        this.authError = "Please enter your Email or Username, and Password.";
        return;
      }
      this.authLoading = true;
      try {
        const res = await fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            identifier: ident,
            username: ident,
            password: this.authForm.password
          })
        });
        const contentType = res.headers.get("content-type") || "";
        if (!contentType.includes("application/json")) {
          throw new Error("Login service currently unreachable. Please verify server connection.");
        }
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.detail || "Invalid login credentials.");
        }
        this.authToken = data.token;
        this.currentUser = data.user;
        localStorage.setItem("agent_auth_token", data.token);
        this.authForm.password = "";
        this.fetchUserHistory();
        this.fetchUserTables();
        this.fetchTableCatalog();
      } catch (err) {
        this.authError = err.message || "Failed to log in.";
      } finally {
        this.authLoading = false;
      }
    },

    async handleRegister() {
      this.authError = null;
      const fullName = (this.authForm.name || this.authForm.displayName || "").trim();
      const email = (this.authForm.email || "").trim();
      if (!fullName) {
        this.authError = "Please enter your Full Name.";
        return;
      }
      if (!email || !email.includes("@")) {
        this.authError = "Please enter a valid Email address.";
        return;
      }
      if (!this.authForm.password || this.authForm.password.length < 4) {
        this.authError = "Password must be at least 4 characters long.";
        return;
      }
      this.authLoading = true;
      try {
        const res = await fetch("/api/auth/register", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: fullName,
            email: email,
            password: this.authForm.password,
            username: this.authForm.username || email.split("@")[0]
          })
        });
        const contentType = res.headers.get("content-type") || "";
        if (!contentType.includes("application/json")) {
          throw new Error("Registration service currently unreachable.");
        }
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.detail || "Registration failed.");
        }
        this.authToken = data.token;
        this.currentUser = data.user;
        localStorage.setItem("agent_auth_token", data.token);
        this.authForm.password = "";
        this.fetchUserHistory();
        this.fetchUserTables();
        this.fetchTableCatalog();
      } catch (err) {
        this.authError = err.message || "Failed to register account.";
      } finally {
        this.authLoading = false;
      }
    },

    async handleLogout() {
      try {
        if (this.authToken) {
          await fetch("/api/auth/logout", {
            method: "POST",
            headers: { "Authorization": "Bearer " + this.authToken }
          });
        }
      } catch (e) {
        // ignore
      }
      this.authToken = "";
      this.currentUser = null;
      this.userHistory = [];
      localStorage.removeItem("agent_auth_token");
    },

    quickDemoLogin(role) {
      if (role === "admin") {
        this.authForm.loginId = "admin@enterprise.ai";
        this.authForm.password = "admin123";
      } else {
        this.authForm.loginId = "demo@enterprise.ai";
        this.authForm.password = "demo123";
      }
      this.handleLogin();
    },

    setDatasetMode(mode) {
      this.datasetMode = mode;
      localStorage.setItem("agent_dataset_mode", mode);
      this.fetchTableCatalog();
    },

    async fetchUserTables() {
      if (!this.authToken) return;
      try {
        const res = await fetch("/api/user/tables", {
          headers: { "Authorization": "Bearer " + this.authToken }
        });
        const contentType = res.headers.get("content-type") || "";
        if (res.ok && contentType.includes("application/json")) {
          const data = await res.json();
          this.userTables = data.tables || [];
        }
      } catch (e) {
        console.warn("Failed fetching user tables:", e);
      }
    },

    handleFileSelect(e) {
      const files = e.target.files;
      if (files && files.length) {
        this.uploadFileObj = files[0];
        if (!this.uploadCustomTableName) {
          const nameWithoutExt = files[0].name.replace(/\.[^/.]+$/, "");
          this.uploadCustomTableName = nameWithoutExt.replace(/[^a-zA-Z0-9_]/g, "_").toLowerCase();
        }
      }
    },

    async submitDatasetUpload() {
      if (!this.uploadFileObj) {
        this.uploadError = "Please select a dataset file (.csv, .xlsx, or .parquet).";
        return;
      }
      this.uploadLoading = true;
      this.uploadError = null;
      this.uploadSuccessMsg = null;

      const formData = new FormData();
      formData.append("file", this.uploadFileObj);
      if (this.uploadCustomTableName) {
        formData.append("table_name", this.uploadCustomTableName);
      }

      try {
        const res = await fetch("/api/upload", {
          method: "POST",
          headers: { "Authorization": "Bearer " + this.authToken },
          body: formData
        });
        const contentType = res.headers.get("content-type") || "";
        if (!contentType.includes("application/json")) {
          throw new Error("Upload endpoint returned an unexpected response.");
        }
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.detail || "Failed to upload dataset.");
        }
        this.uploadSuccessMsg = `Table '${data.dataset.table_name}' (${data.dataset.row_count.toLocaleString()} rows) imported successfully!`;
        this.uploadFileObj = null;
        this.uploadCustomTableName = "";
        await this.fetchUserTables();
        this.setDatasetMode("user");
        setTimeout(() => {
          this.showUploadModal = false;
          this.uploadSuccessMsg = null;
        }, 1200);
      } catch (err) {
        this.uploadError = err.message || "Failed to process upload.";
      } finally {
        this.uploadLoading = false;
      }
    },

    async deleteTable(tblName) {
      if (!confirm(`Are you sure you want to delete table '${tblName}'?`)) return;
      try {
        const res = await fetch(`/api/user/tables/${tblName}`, {
          method: "DELETE",
          headers: { "Authorization": "Bearer " + this.authToken }
        });
        if (res.ok) {
          await this.fetchUserTables();
          await this.fetchTableCatalog();
        }
      } catch (e) {
        console.warn("Failed deleting table:", e);
      }
    },

    startNewSession() {
      this.sessionId = "sess_" + Math.random().toString(36).substring(2, 9);
      localStorage.setItem("agent_session_id", this.sessionId);
      this.query = "";
      this.resetStages();
      this.error = null;
    },

    async fetchUserHistory() {
      if (!this.authToken) return;
      this.historyLoading = true;
      try {
        const res = await fetch("/api/user/history?limit=100", {
          headers: { "Authorization": "Bearer " + this.authToken }
        });
        if (res.ok) {
          const data = await res.json();
          this.userHistory = data.history || [];
        }
      } catch (err) {
        console.warn("Failed fetching user history:", err);
      } finally {
        this.historyLoading = false;
      }
    },

    async clearMyHistory() {
      if (!confirm("Are you sure you want to clear your session and query history?")) return;
      try {
        const res = await fetch("/api/user/history", {
          method: "DELETE",
          headers: { "Authorization": "Bearer " + this.authToken }
        });
        if (res.ok) {
          this.userHistory = [];
        }
      } catch (err) {
        console.warn("Failed clearing history:", err);
      }
    },

    loadHistoryItem(item) {
      this.query = item.query;
      if (item.result && (item.result.data || item.result.generated_sql || item.result.final_response)) {
        this.result = Object.assign({}, this.result, item.result);
        this.chartOptions = item.result.chart_options || [];
        this.selectedChartIndex = 0;
        Object.keys(this.stages).forEach((k) => {
          this.stages[k].status = "done";
        });
        this.setTab("summary");
      }
    }
  }
}).mount("#app");
