const app = document.getElementById("app");
const params = new URLSearchParams(window.location.search);
const apiUrlParam = (params.get("api") || "").replace(/\/+$/, "");
if (apiUrlParam) {
  localStorage.setItem("fariksApiBase", apiUrlParam);
}
const API_BASE =
  (window.FARIKS_API_BASE || "").replace(/\/+$/, "") ||
  apiUrlParam ||
  localStorage.getItem("fariksApiBase") ||
  (window.location.protocol === "file:" ? "http://localhost:8080" : window.location.origin);
const adminUrlToken = params.get("token") || "";
if (adminUrlToken) {
  localStorage.setItem("fariksAdminToken", adminUrlToken);
}

const testState = {
  slug: "",
  token: "",
  payload: null,
  status: "loading",
  current: 0,
  answers: {},
  deadline: 0,
  timer: null,
  notice: "",
  result: null,
};

const adminState = {
  token: adminUrlToken || localStorage.getItem("fariksAdminToken") || "",
  loading: true,
  message: "",
  error: "",
  me: null,
  summary: null,
  courses: [],
  students: [],
  payments: [],
  results: [],
};

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function money(amount) {
  return `${Number(amount || 0).toLocaleString("uz-UZ")} so'm`;
}

function typeset() {
  window.requestAnimationFrame(() => {
    if (window.MathJax?.typesetPromise) {
      window.MathJax.typesetPromise().catch(() => {});
    }
  });
}

async function apiGet(path, admin = false) {
  const headers = admin ? { "X-Admin-Token": adminState.token } : {};
  const response = await fetch(`${API_BASE}${path}`, { headers });
  return parseApiResponse(response);
}

async function apiPost(path, body, admin = false) {
  const headers = { "Content-Type": "application/json" };
  if (admin) headers["X-Admin-Token"] = adminState.token;
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  return parseApiResponse(response);
}

async function parseApiResponse(response) {
  const text = await response.text();
  let payload = null;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new Error(`API javobi noto'g'ri (${response.status}). Backend domenini tekshiring.`);
  }
  if (!response.ok || !payload.ok) {
    throw new Error(payload.error || `So'rov bajarilmadi (${response.status})`);
  }
  return payload.data;
}

function route() {
  if (window.location.pathname.startsWith("/admin")) {
    initAdmin();
    return;
  }
  initTest();
}

async function initTest() {
  const parts = window.location.pathname.split("/").filter(Boolean);
  testState.slug = parts[0] === "test" && parts[1] ? decodeURIComponent(parts[1]) : params.get("lesson") || "";
  testState.token = params.get("token") || "";

  if (!testState.slug || !testState.token) {
    testState.status = "no-access";
    renderTest();
    return;
  }

  try {
    testState.payload = await apiGet(`/api/test/${encodeURIComponent(testState.slug)}?token=${encodeURIComponent(testState.token)}`);
    testState.status = "intro";
    renderTest();
  } catch (error) {
    testState.status = "error";
    testState.notice = error.message;
    renderTest();
  }
}

function renderShell(content, subtitle = "Online test platformasi") {
  app.innerHTML = `
    <div class="app-shell">
      <header class="topbar">
        <div class="topbar-inner">
          <div class="brand">
            <div class="mark">F</div>
            <div>
              <strong>FARIKS LMS</strong>
              <span>${escapeHtml(subtitle)}</span>
            </div>
          </div>
        </div>
      </header>
      <main class="layout">${content}</main>
    </div>
  `;
}

function renderTest() {
  clearInterval(testState.timer);

  if (testState.status === "no-access") {
    renderShell(`
      <section class="empty-state">
        <div class="panel pad empty-card">
          <p class="eyebrow">Kirish</p>
          <h1>Test link Telegram bot orqali ochiladi</h1>
          <p class="muted">Qayta ro'yxatdan o'tish kerak emas. Bot yuborgan maxsus link token bilan avtomatik login qiladi.</p>
        </div>
      </section>
    `);
    return;
  }

  if (testState.status === "error") {
    renderShell(`
      <section class="empty-state">
        <div class="panel pad empty-card">
          <p class="eyebrow">Xatolik</p>
          <h1>Test ochilmadi</h1>
          <p class="muted">${escapeHtml(testState.notice)}</p>
        </div>
      </section>
    `);
    return;
  }

  if (testState.status === "intro") {
    const data = testState.payload;
    renderShell(`
      <section class="panel pad">
        <p class="eyebrow">${escapeHtml(data.course.title)}</p>
        <h1>${escapeHtml(data.lesson.title)}</h1>
        <p class="muted">${escapeHtml(data.user.full_name)} uchun test sessiyasi tayyor.</p>

        <div class="meta-row">
          <div class="metric"><span>Savollar soni</span><strong>${data.questions.length} ta</strong></div>
          <div class="metric"><span>Vaqt</span><strong>${data.lesson.duration_minutes} daqiqa</strong></div>
          <div class="metric"><span>O'tish bali</span><strong>${data.lesson.pass_percent}%</strong></div>
        </div>

        <div class="button-row">
          <button class="btn success" id="startTest">Boshlash</button>
        </div>
      </section>
    `);
    document.getElementById("startTest").addEventListener("click", startTest);
    return;
  }

  if (testState.status === "active") {
    renderActiveTest();
    return;
  }

  if (testState.status === "done") {
    renderResult();
  }
}

function startTest() {
  testState.status = "active";
  testState.current = 0;
  testState.answers = {};
  testState.notice = "";
  testState.deadline = Date.now() + testState.payload.lesson.duration_minutes * 60 * 1000;
  renderActiveTest();
}

function renderActiveTest() {
  const data = testState.payload;
  const questions = data.questions;
  const current = questions[testState.current];
  const answeredCount = Object.keys(testState.answers).length;
  const missingCount = questions.length - answeredCount;
  const canFinish = missingCount === 0;
  const percent = Math.round((answeredCount * 100) / questions.length);

  renderShell(`
    <div class="test-grid">
      <section class="panel question-panel">
        <div class="question-head">
          <div>
            <p class="eyebrow">${escapeHtml(data.module.title)}</p>
            <h2>${escapeHtml(data.lesson.title)}</h2>
          </div>
          <span class="status-pill">${testState.current + 1}/${questions.length}</span>
        </div>
        <div class="progress-track"><div class="progress-bar" style="width:${percent}%"></div></div>
        <div class="question-body">
          <div class="question-text">${escapeHtml(current.text)}</div>
          <div class="options">
            ${["A", "B", "C", "D"]
              .map((key) => {
                const selected = testState.answers[current.id] === key ? " selected" : "";
                return `
                  <button class="option${selected}" data-option="${key}">
                    <span class="option-key">${key}</span>
                    <span>${escapeHtml(current.options[key])}</span>
                  </button>
                `;
              })
              .join("")}
          </div>
          ${testState.notice ? `<div class="notice">${escapeHtml(testState.notice)}</div>` : ""}
        </div>
        <div class="question-footer">
          <div class="button-row">
            <button class="btn secondary" id="prevQuestion" ${testState.current === 0 ? "disabled" : ""}>Oldingi</button>
            <button class="btn secondary" id="nextQuestion" ${testState.current === questions.length - 1 ? "disabled" : ""}>Keyingi</button>
          </div>
          <div class="finish-wrap">
            <span class="finish-hint">${canFinish ? "Barcha savollar belgilandi" : `${missingCount} ta savol javobsiz`}</span>
            <button class="btn success" id="finishTest" ${canFinish ? "" : "disabled"}>Yakunlash</button>
          </div>
        </div>
      </section>

      <aside class="panel pad side-panel">
        <div class="timer">
          <span class="muted">Qolgan vaqt</span>
          <strong id="timerValue">${formatTime(timeLeft())}</strong>
        </div>
        <div class="question-map">
          ${questions
            .map((question, index) => {
              const classes = [
                "q-dot",
                index === testState.current ? "current" : "",
                testState.answers[question.id] ? "answered" : "",
              ]
                .filter(Boolean)
                .join(" ");
              return `<button class="${classes}" data-goto="${index}">${index + 1}</button>`;
            })
            .join("")}
        </div>
      </aside>
    </div>
  `);

  document.querySelectorAll("[data-option]").forEach((button) => {
    button.addEventListener("click", () => {
      testState.answers[current.id] = button.dataset.option;
      testState.notice = "";
      renderActiveTest();
    });
  });
  document.querySelectorAll("[data-goto]").forEach((button) => {
    button.addEventListener("click", () => {
      testState.current = Number(button.dataset.goto);
      testState.notice = "";
      renderActiveTest();
    });
  });
  document.getElementById("prevQuestion").addEventListener("click", () => {
    testState.current = Math.max(0, testState.current - 1);
    testState.notice = "";
    renderActiveTest();
  });
  document.getElementById("nextQuestion").addEventListener("click", () => {
    testState.current = Math.min(questions.length - 1, testState.current + 1);
    testState.notice = "";
    renderActiveTest();
  });
  document.getElementById("finishTest").addEventListener("click", submitTest);

  testState.timer = setInterval(updateTimer, 1000);
  typeset();
}

function timeLeft() {
  return Math.max(0, Math.ceil((testState.deadline - Date.now()) / 1000));
}

function formatTime(totalSeconds) {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function updateTimer() {
  const node = document.getElementById("timerValue");
  if (node) node.textContent = formatTime(timeLeft());
  if (timeLeft() <= 0) submitTest(true);
}

async function submitTest(force = false) {
  const total = testState.payload.questions.length;
  const answered = Object.keys(testState.answers).length;
  if (!force && answered < total) {
    testState.notice = `Hali ${total - answered} ta savol javobsiz.`;
    renderActiveTest();
    return;
  }

  clearInterval(testState.timer);
  try {
    const result = await apiPost("/api/results", {
      token: testState.token,
      answers: testState.answers,
    });
    testState.result = result;
    testState.status = "done";
    renderResult();
  } catch (error) {
    testState.notice = error.message;
    testState.status = "active";
    renderActiveTest();
  }
}

function renderResult() {
  const result = testState.result;
  const passed = result.passed;
  const scoreStyle = `--score:${result.percent}%`;
  const nextLine = passed
    ? result.course_completed
      ? "Kurs yakunlandi. Sertifikat uchun admin bilan bog'laning."
      : "Keyingi dars Telegram botda ochildi."
    : "Darsni qayta ko'rib, testni yana urinib ko'ring.";

  renderShell(`
    <section class="panel pad">
      <div class="result-hero">
        <div class="score-circle" style="${scoreStyle}"><span>${result.percent}%</span></div>
        <div>
          <span class="status-pill ${passed ? "" : "bad"}">${passed ? "Testdan o'tdingiz" : "Qayta urinish kerak"}</span>
          <h1>${passed ? "Tabriklaymiz!" : "Testdan o'ta olmadingiz"}</h1>
          <p class="muted">${escapeHtml(nextLine)}</p>
        </div>
      </div>
      <div class="meta-row">
        <div class="metric"><span>To'g'ri javob</span><strong>${result.correct_count}/${result.total_count}</strong></div>
        <div class="metric"><span>Noto'g'ri</span><strong>${result.wrong_count}</strong></div>
        <div class="metric"><span>Javobsiz</span><strong>${result.unanswered_count}</strong></div>
      </div>
      <div class="meta-row">
        <div class="metric"><span>Natija</span><strong>${result.percent}%</strong></div>
        <div class="metric"><span>Kerakli bal</span><strong>${result.pass_percent}%</strong></div>
        <div class="metric"><span>Holat</span><strong>${passed ? "O'tdi" : "O'tmadi"}</strong></div>
      </div>
      ${renderResultDetails(result)}
    </section>
  `);
  typeset();
}

function renderResultDetails(result) {
  if (!result.details?.length) return "";
  return `
    <div class="review-list">
      <h2>Javoblar tahlili</h2>
      ${result.details.map(renderResultItem).join("")}
    </div>
  `;
}

function renderResultItem(detail) {
  const statusClass = detail.is_correct ? "correct" : "wrong";
  const selectedLabel = detail.selected
    ? `${detail.selected}) ${detail.selected_text}`
    : "Javob berilmagan";
  const correctLabel = `${detail.correct}) ${detail.correct_text}`;
  return `
    <article class="review-item ${statusClass}">
      <div class="review-head">
        <span class="answer-badge ${statusClass}">${detail.is_correct ? "To'g'ri" : "Noto'g'ri"}</span>
        <strong>${detail.position}-savol</strong>
      </div>
      <div class="review-question">${escapeHtml(detail.text)}</div>
      <div class="answer-pair">
        <div>
          <span>Sizning javobingiz</span>
          <strong>${escapeHtml(selectedLabel)}</strong>
        </div>
        <div>
          <span>To'g'ri javob</span>
          <strong>${escapeHtml(correctLabel)}</strong>
        </div>
      </div>
      ${detail.explanation ? `<p class="explanation">${escapeHtml(detail.explanation)}</p>` : ""}
    </article>
  `;
}

async function initAdmin() {
  if (adminUrlToken || apiUrlParam) {
    window.history.replaceState({}, "", "/admin");
  }
  renderAdmin();
  await loadAdminData();
}

async function loadAdminData() {
  adminState.loading = true;
  adminState.error = "";
  renderAdmin();
  try {
    const [me, summary, courses, students, payments, results] = await Promise.all([
      apiGet("/api/admin/me", true),
      apiGet("/api/admin/summary", true),
      apiGet("/api/admin/courses", true),
      apiGet("/api/admin/students", true),
      apiGet("/api/admin/payments", true),
      apiGet("/api/admin/results", true),
    ]);
    Object.assign(adminState, { me, summary, courses, students, payments, results, loading: false });
  } catch (error) {
    adminState.error = error.message;
    adminState.loading = false;
  }
  renderAdmin();
}

function allModules() {
  return adminState.courses.flatMap((course) =>
    course.modules.map((module) => ({ ...module, course_title: course.title })),
  );
}

function allLessons() {
  return adminState.courses.flatMap((course) =>
    course.modules.flatMap((module) =>
      module.lessons.map((lesson) => ({
        ...lesson,
        module_title: module.title,
        course_title: course.title,
      })),
    ),
  );
}

function emptyOption(label) {
  return `<option value="" disabled selected>${escapeHtml(label)}</option>`;
}

function renderCourseOptions(courses) {
  if (!courses.length) return emptyOption("Kurs yo'q");
  return courses.map((course) => `<option value="${course.id}">${escapeHtml(course.title)}</option>`).join("");
}

function renderModuleOptions(modules) {
  if (!modules.length) return emptyOption("Modul yo'q");
  return modules
    .map((module) => `<option value="${module.id}">${escapeHtml(module.course_title)} / ${escapeHtml(module.title)}</option>`)
    .join("");
}

function renderLessonOptions(lessons) {
  if (!lessons.length) return emptyOption("Dars yo'q");
  return lessons
    .map((lesson) => `<option value="${lesson.id}">${escapeHtml(lesson.course_title)} / ${escapeHtml(lesson.title)}</option>`)
    .join("");
}

const formulaTemplates = [
  {
    id: "none",
    label: "Formula yo'q",
    title: "Oddiy savol",
    fields: [],
    build: () => "",
  },
  {
    id: "fraction",
    label: "Kasr",
    title: "Kasrli tenglama",
    fields: [
      ["top", "Kasr usti", "2x+3"],
      ["bottom", "Kasr osti", "x-1"],
      ["right", "Tenglikdan keyin", "5"],
    ],
    build: (v) => `\\frac{${v.top}}{${v.bottom}}=${v.right}`,
  },
  {
    id: "sqrt",
    label: "Ildiz",
    title: "Ildizli tenglama",
    fields: [
      ["first", "1-ildiz ichi", "x+4"],
      ["second", "2-ildiz ichi", "x-1"],
      ["right", "Tenglikdan keyin", "5"],
    ],
    build: (v) => `\\sqrt{${v.first}}+\\sqrt{${v.second}}=${v.right}`,
  },
  {
    id: "power",
    label: "Daraja",
    title: "Darajali ifoda",
    fields: [
      ["base", "Asos", "x"],
      ["degree", "Daraja", "2"],
      ["extra", "Davomi", "+3x+2"],
      ["right", "Tenglikdan keyin", "0"],
    ],
    build: (v) => `${v.base}^{${v.degree}}${v.extra || ""}=${v.right}`,
  },
  {
    id: "log",
    label: "Log",
    title: "Logarifm",
    fields: [
      ["base", "Log asos", "2"],
      ["inside", "Log ichida", "x+1"],
      ["right", "Tenglikdan keyin", "3"],
    ],
    build: (v) => `\\log_{${v.base}}\\left(${v.inside}\\right)=${v.right}`,
  },
  {
    id: "trig",
    label: "Trigonometria",
    title: "Trigonometrik tenglama",
    fields: [
      ["fn", "Funksiya", "sin"],
      ["angle", "Burchak yoki ifoda", "x"],
      ["right", "Tenglikdan keyin", "0"],
    ],
    build: (v) => `\\${normalizeTrigName(v.fn)}\\left(${v.angle}\\right)=${v.right}`,
  },
];

function normalizeTrigName(value) {
  const name = String(value || "sin").trim().toLowerCase();
  if (name === "tg" || name === "tan") return "tan";
  if (name === "ctg" || name === "cot") return "cot";
  if (name === "cos") return "cos";
  return "sin";
}

function renderFormulaTools() {
  return formulaTemplates
    .map(
      (item, index) => `
        <button class="formula-tool ${index === 0 ? "active" : ""}" type="button" data-formula-template="${item.id}">
          ${escapeHtml(item.label)}
        </button>
      `,
    )
    .join("");
}

function renderFormulaFields() {
  return formulaTemplates
    .filter((item) => item.id !== "none")
    .map(
      (item) => `
        <div class="formula-fields ${item.id === "fraction" ? "active" : ""}" data-formula-panel="${item.id}">
          <div class="formula-title">${escapeHtml(item.title)}</div>
          <div class="formula-input-grid">
            ${item.fields
              .map(
                ([key, label, placeholder]) => `
                  <div class="field">
                    <label>${escapeHtml(label)}</label>
                    <input data-formula-input="${item.id}:${key}" placeholder="${escapeHtml(placeholder)}" />
                  </div>
                `,
              )
              .join("")}
          </div>
        </div>
      `,
    )
    .join("");
}

function activeFormulaTemplate() {
  const active = document.querySelector("[data-formula-template].active");
  return formulaTemplates.find((item) => item.id === active?.dataset.formulaTemplate) || formulaTemplates[0];
}

function formulaValues(template) {
  return Object.fromEntries(
    template.fields.map(([key]) => [
      key,
      document.querySelector(`[data-formula-input="${template.id}:${key}"]`)?.value.trim() || "",
    ]),
  );
}

function buildFormulaLatex() {
  const template = activeFormulaTemplate();
  if (template.id === "none") return "";
  const values = formulaValues(template);
  const required = template.fields.filter(([key]) => key !== "extra").every(([key]) => values[key]);
  return required ? template.build(values) : "";
}

function buildQuestionText() {
  const intro = document.getElementById("questionIntro")?.value.trim() || "";
  const latex = buildFormulaLatex();
  if (!latex) return intro;
  return [intro, `\\[${latex}\\]`].filter(Boolean).join("\n");
}

function updateQuestionPreview() {
  const preview = document.getElementById("questionPreview");
  if (!preview) return;
  const text = buildQuestionText() || "Savol ko'rinishi shu yerda chiqadi.";
  const options = ["A", "B", "C", "D"]
    .map((key) => {
      const value = document.querySelector(`[name="option_${key.toLowerCase()}"]`)?.value.trim() || "...";
      return `<div><strong>${key}</strong><span>${escapeHtml(value)}</span></div>`;
    })
    .join("");
  preview.innerHTML = `
    <div class="preview-question">${escapeHtml(text)}</div>
    <div class="preview-options">${options}</div>
  `;
  typeset();
}

function bindFormulaBuilder() {
  const tools = [...document.querySelectorAll("[data-formula-template]")];
  if (!tools.length) return;
  const syncPanels = () => {
    const template = activeFormulaTemplate();
    document.querySelectorAll("[data-formula-panel]").forEach((panel) => {
      panel.classList.toggle("active", panel.dataset.formulaPanel === template.id);
    });
    updateQuestionPreview();
  };
  tools.forEach((button) => {
    button.addEventListener("click", () => {
      tools.forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      syncPanels();
    });
  });
  document.querySelectorAll("#questionForm input, #questionForm textarea, #questionForm select").forEach((field) => {
    field.addEventListener("input", updateQuestionPreview);
    field.addEventListener("change", updateQuestionPreview);
  });
  syncPanels();
}

function renderAdminAccessPanel() {
  if (adminState.token && !adminState.error) {
    const name = adminState.me?.name || "Admin";
    const login = adminState.me?.login_method === "telegram" ? "Telegram orqali kirildi" : "Admin kabinet";
    return `
      <section class="panel pad">
        <h2>Admin kabinet</h2>
        <p class="muted">${escapeHtml(login)}</p>
        <div class="metric"><span>Admin</span><strong>${escapeHtml(name)}</strong></div>
        <button class="btn secondary" id="logoutAdmin">Chiqish</button>
      </section>
    `;
  }

  return `
    <section class="panel pad">
      <h2>Admin kirish</h2>
      <div class="form-grid">
        <div class="field">
          <label>Admin token</label>
          <input id="adminToken" value="${escapeHtml(adminState.token)}" />
        </div>
        <button class="btn" id="saveAdminToken">Saqlash</button>
      </div>
    </section>
  `;
}

function renderAdmin() {
  const summary = adminState.summary || {};
  const modules = allModules();
  const lessons = allLessons();
  const hasCourses = adminState.courses.length > 0;
  const hasModules = modules.length > 0;
  const hasLessons = lessons.length > 0;
  renderShell(`
    <div class="admin-grid">
      <aside class="admin-stack">
        ${renderAdminAccessPanel()}

        <section class="panel pad admin-form-card">
          <div class="form-title">
            <span class="step-badge">1</span>
            <div>
              <h2>Kurs yaratish</h2>
              <p class="muted">Kurs nomi, narxi va qisqa tavsifi.</p>
            </div>
          </div>
          <form class="form-grid" id="courseForm">
            <div class="field"><label>Kurs nomi</label><input name="title" required placeholder="Milliy Sertifikat Matematika" /></div>
            <div class="field"><label>Narxi</label><input name="price" type="number" min="1" required placeholder="300000" /></div>
            <div class="field"><label>Tavsif</label><textarea name="description" placeholder="Kurs kimlar uchun va nimani o'rgatadi"></textarea></div>
            <button class="btn success">Kursni saqlash</button>
          </form>
        </section>

        <section class="panel pad admin-form-card">
          <div class="form-title">
            <span class="step-badge">2</span>
            <div>
              <h2>Modul yaratish</h2>
              <p class="muted">Modul tanlangan kurs ichida ko'rinadi.</p>
            </div>
          </div>
          <form class="form-grid" id="moduleForm">
            <div class="field">
              <label>Kurs</label>
              <select name="course_id" required ${hasCourses ? "" : "disabled"}>
                ${renderCourseOptions(adminState.courses)}
              </select>
            </div>
            <div class="field"><label>Modul nomi</label><input name="title" required placeholder="1-MODUL: Algebra asoslari" /></div>
            <div class="field"><label>Tartib raqami</label><input name="position" type="number" value="1" min="1" /></div>
            ${hasCourses ? "" : `<p class="empty-hint">Avval kurs yarating.</p>`}
            <button class="btn success" ${hasCourses ? "" : "disabled"}>Modulni saqlash</button>
          </form>
        </section>
      </aside>

      <section class="admin-stack">
        ${adminState.error ? `<div class="message error">${escapeHtml(adminState.error)}</div>` : ""}
        ${adminState.message ? `<div class="message">${escapeHtml(adminState.message)}</div>` : ""}
        ${
          adminState.loading
            ? `<section class="panel pad"><h2>Yuklanmoqda...</h2></section>`
            : `
              <section class="stats-grid">
                ${statCard("Kurslar", summary.courses)}
                ${statCard("Darslar", summary.lessons)}
                ${statCard("O'quvchilar", summary.students)}
                ${statCard("Natijalar", summary.results)}
              </section>

              <section class="panel pad admin-form-card">
                <div class="form-title">
                  <span class="step-badge">3</span>
                  <div>
                    <h2>Dars qo'shish</h2>
                    <p class="muted">Dars modul ichida ochiladi, test foizi keyingi darsni ochadi.</p>
                  </div>
                </div>
                <form class="form-grid" id="lessonForm">
                  <div class="split">
                    <div class="field">
                      <label>Modul</label>
                      <select name="module_id" required ${hasModules ? "" : "disabled"}>
                        ${renderModuleOptions(modules)}
                      </select>
                    </div>
                    <div class="field"><label>Tartib raqami</label><input name="position" type="number" value="1" min="1" /></div>
                  </div>
                  <div class="field"><label>Dars nomi</label><input name="title" required placeholder="1-Dars: Chiziqli tenglamalar" /></div>
                  <div class="field"><label>Video URL</label><input name="video_url" placeholder="https://..." /></div>
                  <div class="split">
                    <div class="field"><label>Vaqt, daqiqa</label><input name="duration_minutes" type="number" value="30" min="1" /></div>
                    <div class="field"><label>O'tish foizi</label><input name="pass_percent" type="number" value="80" min="1" max="100" /></div>
                  </div>
                  ${hasModules ? "" : `<p class="empty-hint">Avval modul yarating.</p>`}
                  <button class="btn success" ${hasModules ? "" : "disabled"}>Darsni saqlash</button>
                </form>
              </section>

              <section class="panel pad admin-form-card">
                <div class="form-title">
                  <span class="step-badge">3A</span>
                  <div>
                    <h2>Dars videosini yangilash</h2>
                    <p class="muted">Mavjud darsga YouTube, Vimeo yoki boshqa video havola kiriting.</p>
                  </div>
                </div>
                <form class="form-grid" id="videoForm">
                  <div class="field">
                    <label>Dars</label>
                    <select name="lesson_id" required ${hasLessons ? "" : "disabled"}>
                      ${renderLessonOptions(lessons)}
                    </select>
                  </div>
                  <div class="field"><label>Video URL</label><input name="video_url" required placeholder="https://..." /></div>
                  ${hasLessons ? "" : `<p class="empty-hint">Avval dars yarating.</p>`}
                  <button class="btn success" ${hasLessons ? "" : "disabled"}>Videoni saqlash</button>
                </form>
              </section>

              <section class="panel pad admin-form-card">
                <div class="form-title">
                  <span class="step-badge">4</span>
                  <div>
                    <h2>Test savoli qo'shish</h2>
                    <p class="muted">Formula kodi kerak emas: tugmani tanlab, bo'sh joylarni to'ldiring.</p>
                  </div>
                </div>
                <form class="form-grid" id="questionForm">
                  <div class="field">
                    <label>Dars</label>
                    <select name="lesson_id" required ${hasLessons ? "" : "disabled"}>
                      ${renderLessonOptions(lessons)}
                    </select>
                  </div>
                  <div class="field">
                    <label>Savol matni</label>
                    <textarea id="questionIntro" name="question_intro" required placeholder="Tenglamani yeching."></textarea>
                  </div>
                  <div class="formula-builder">
                    <label>Formula qo'shish</label>
                    <div class="formula-toolbar">${renderFormulaTools()}</div>
                    ${renderFormulaFields()}
                  </div>
                  <div class="answer-grid">
                    <div class="field"><label>A variant</label><input name="option_a" required placeholder="4" /></div>
                    <div class="field"><label>B variant</label><input name="option_b" required placeholder="5" /></div>
                    <div class="field"><label>C variant</label><input name="option_c" required placeholder="8/3" /></div>
                    <div class="field"><label>D variant</label><input name="option_d" required placeholder="-1" /></div>
                  </div>
                  <div class="split">
                    <div class="field">
                      <label>To'g'ri javob</label>
                      <select name="correct_option"><option>A</option><option>B</option><option>C</option><option>D</option></select>
                    </div>
                    <div class="field"><label>Tartib raqami</label><input name="position" type="number" value="1" min="1" /></div>
                  </div>
                  <div class="field"><label>Izoh</label><textarea name="explanation" placeholder="Tenglamani yechganda x=8/3 chiqadi."></textarea></div>
                  <div class="question-preview" id="questionPreview"></div>
                  ${hasLessons ? "" : `<p class="empty-hint">Avval dars yarating.</p>`}
                  <button class="btn success" ${hasLessons ? "" : "disabled"}>Savolni saqlash</button>
                </form>
              </section>

              <section class="panel pad">
                <h2>Kurs tuzilmasi</h2>
                <div class="course-tree">
                  ${adminState.courses.map(renderCourseTree).join("")}
                </div>
              </section>

              <section class="panel pad">
                <h2>O'quvchilar</h2>
                ${renderStudentsTable()}
              </section>

              <section class="panel pad">
                <h2>To'lovlar</h2>
                ${renderPaymentsTable()}
              </section>

              <section class="panel pad">
                <h2>Natijalar</h2>
                ${renderResultsTable()}
              </section>
            `
        }
      </section>
    </div>
  `, "Admin panel");

  bindAdminEvents();
  typeset();
}

function statCard(label, value) {
  return `<div class="panel pad metric"><span>${escapeHtml(label)}</span><strong>${Number(value || 0)}</strong></div>`;
}

function renderCourseTree(course) {
  return `
    <div class="course-item">
      <div class="course-title">
        <span>${escapeHtml(course.title)}</span>
        <span>${money(course.price)}</span>
      </div>
      <ul class="module-list">
        ${course.modules
          .map(
            (module) => `
              <li>
                ${escapeHtml(module.title)}
                <ul class="lesson-list">
                  ${module.lessons
                    .map((lesson) => `<li>${escapeHtml(lesson.title)} - ${lesson.question_count} savol</li>`)
                    .join("")}
                </ul>
              </li>
            `,
          )
          .join("")}
      </ul>
    </div>
  `;
}

function renderStudentsTable() {
  if (!adminState.students.length) return `<p class="muted">Hali o'quvchilar yo'q.</p>`;
  return `
    <div class="table-wrap">
      <table>
        <thead><tr><th>Ism</th><th>Telefon</th><th>Kurslar</th><th>O'tilgan darslar</th></tr></thead>
        <tbody>
          ${adminState.students
            .map(
              (student) => `
                <tr>
                  <td>${escapeHtml(student.full_name)}</td>
                  <td>${escapeHtml(student.phone)}</td>
                  <td>${student.courses_count}</td>
                  <td>${student.passed_lessons}</td>
                </tr>
              `,
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderPaymentsTable() {
  if (!adminState.payments.length) return `<p class="muted">To'lovlar hali yo'q.</p>`;
  return `
    <div class="table-wrap">
      <table>
        <thead><tr><th>O'quvchi</th><th>Kurs</th><th>Usul</th><th>Summa</th><th>Status</th></tr></thead>
        <tbody>
          ${adminState.payments
            .map(
              (payment) => `
                <tr>
                  <td>${escapeHtml(payment.full_name)}</td>
                  <td>${escapeHtml(payment.course_title)}</td>
                  <td>${escapeHtml(payment.method)}</td>
                  <td>${money(payment.amount)}</td>
                  <td>${escapeHtml(payment.status)}</td>
                </tr>
              `,
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderResultsTable() {
  if (!adminState.results.length) return `<p class="muted">Natijalar hali yo'q.</p>`;
  return `
    <div class="table-wrap">
      <table>
        <thead><tr><th>O'quvchi</th><th>Dars</th><th>Natija</th><th>Holat</th></tr></thead>
        <tbody>
          ${adminState.results
            .map(
              (result) => `
                <tr>
                  <td>${escapeHtml(result.full_name)}</td>
                  <td>${escapeHtml(result.lesson_title)}</td>
                  <td>${result.correct_count}/${result.total_count} - ${result.percent}%</td>
                  <td>${result.passed ? "O'tdi" : "O'tmadi"}</td>
                </tr>
              `,
            )
            .join("")}
        </tbody>
      </table>
    </div>
  `;
}

function bindAdminEvents() {
  const logoutAdmin = document.getElementById("logoutAdmin");
  if (logoutAdmin) {
    logoutAdmin.addEventListener("click", async () => {
      localStorage.removeItem("fariksAdminToken");
      adminState.token = "";
      adminState.me = null;
      adminState.message = "";
      adminState.error = "";
      renderAdmin();
    });
  }

  const saveToken = document.getElementById("saveAdminToken");
  if (saveToken) {
    saveToken.addEventListener("click", async () => {
      adminState.token = document.getElementById("adminToken").value.trim();
      localStorage.setItem("fariksAdminToken", adminState.token);
      await loadAdminData();
      if (!adminState.error) {
        adminState.message = "Admin token saqlandi.";
        renderAdmin();
      }
    });
  }

  bindForm("courseForm", async (data) => {
    await apiPost("/api/admin/courses", data, true);
    adminState.message = "Kurs qo'shildi.";
    await loadAdminData();
  });
  bindForm("moduleForm", async (data) => {
    await apiPost("/api/admin/modules", data, true);
    adminState.message = "Modul qo'shildi.";
    await loadAdminData();
  });
  bindForm("lessonForm", async (data) => {
    await apiPost("/api/admin/lessons", data, true);
    adminState.message = "Dars qo'shildi.";
    await loadAdminData();
  });
  bindForm("videoForm", async (data) => {
    await apiPost("/api/admin/lessons/video", data, true);
    adminState.message = "Dars videosi yangilandi.";
    await loadAdminData();
  });
  bindForm("questionForm", async (data) => {
    if (activeFormulaTemplate().id !== "none" && !buildFormulaLatex()) {
      throw new Error("Formula uchun ochilgan maydonlarni to'liq to'ldiring.");
    }
    data.text = buildQuestionText();
    delete data.question_intro;
    await apiPost("/api/admin/questions", data, true);
    adminState.message = "Savol qo'shildi.";
    await loadAdminData();
  });
  bindFormulaBuilder();
}

function bindForm(id, handler) {
  const form = document.getElementById(id);
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    adminState.error = "";
    const data = Object.fromEntries(new FormData(form).entries());
    try {
      await handler(data);
      form.reset();
    } catch (error) {
      adminState.error = error.message;
      renderAdmin();
    }
  });
}

route();
