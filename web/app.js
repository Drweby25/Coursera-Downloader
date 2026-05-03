const form = document.querySelector("#downloadForm");
const languageSelect = document.querySelector("#subtitle_language");
const courseInput = document.querySelector("#course");
const pathInput = document.querySelector("#path");
const versionLabel = document.querySelector("#version");
const messageBox = document.querySelector("#message");
const logs = document.querySelector("#logs");
const activityTree = document.querySelector("#activityTree");
const jobState = document.querySelector("#jobState");
const downloadBtn = document.querySelector("#downloadBtn");
const pauseBtn = document.querySelector("#pauseBtn");
const cancelBtn = document.querySelector("#cancelBtn");
const refreshBtn = document.querySelector("#refreshBtn");
const selectFolderBtn = document.querySelector("#selectFolderBtn");
const loginCheckBtn = document.querySelector("#loginCheckBtn");
const settingsToggleBtn = document.querySelector("#settingsToggleBtn");
const settingsPanel = document.querySelector("#settingsPanel");
const expandTreeBtn = document.querySelector("#expandTreeBtn");
const collapseTreeBtn = document.querySelector("#collapseTreeBtn");
const courseClearBtn = document.querySelector("#courseClearBtn");
const recentCoursesSection = document.querySelector("#recentCourses");
const recentCoursesGrid = document.querySelector("#recentCoursesGrid");
const activityBody = document.querySelector("#activityBody");

let statusTimer = null;
let loginOpened = false;
let authPollTimer = null;
let loginWindow = null;
let currentStatus = { running: false, status: "idle" };
let canResumeCurrentCourse = false;
let authCheckPromise = null;
let statusMessages = [];
let selectedTreeId = "";
let selectedTreeView = "content";
let currentTreeNodes = [];
let currentSidebarNodes = [];
let downloadedActivityGroups = [];
let lastSessionLogGroups = [];
let activeTreeId = "";
let userChangedExpansion = false;
let autoExpandedInitialTree = false;
let lastDownloadStateAt = 0;
let downloadStatePromise = null;
const expandedTreeIds = new Set();

const AUTH_STORAGE_KEY = "courseraDownloader.authStatus";
const SETTINGS_STORAGE_KEY = "courseraDownloader.settingsVisible.v3";
const COURSE_INPUT_STORAGE_KEY = "courseraDownloader.lastCourseInput.v3";
console.info("Coursera Downloader UI loaded: 20260503-no-fallback-tree");

function treeNeedsStateRender() {
  return (
    !currentSidebarNodes.length ||
    /Waiting for site tree manifest|No activity yet/i.test(activityTree.textContent || "")
  );
}

function showMessage(text, ok = false) {
  const message = cleanDisplayMessage(text);
  messageBox.hidden = true;
  messageBox.textContent = "";
  if (!message) return;
  statusMessages.push({ text: message, ok, time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) });
  statusMessages = statusMessages.slice(-20);
  renderInlineStatusMessages();
}

function clearMessage() {
  messageBox.hidden = true;
  messageBox.textContent = "";
  messageBox.classList.remove("ok");
}

function statusMessagesHtml() {
  return statusMessages.map((item) => `
    <div class="log-status ${item.ok ? "ok" : "bad"}">
      <span>${escapeHtml(item.time)}</span>
      <p>${escapeHtml(item.text)}</p>
    </div>
  `).join("");
}

function renderInlineStatusMessages() {
  const existingCourses = logs.querySelectorAll(".folder-detail").length;
  if (existingCourses) {
    logs.querySelectorAll(".log-status").forEach((item) => item.remove());
    logs.insertAdjacentHTML("afterbegin", statusMessagesHtml());
    return;
  }
  logs.innerHTML = statusMessagesHtml() || "No download has started.";
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Request failed.");
  }
  return data;
}

function fillSelect(select, items, selected) {
  select.innerHTML = "";
  items.forEach((item) => {
    const option = document.createElement("option");
    option.value = item;
    option.textContent = item;
    option.selected = item === selected;
    select.append(option);
  });
}

function payloadFromForm() {
  const formData = new FormData(form);
  return {
    course: formData.get("course"),
    path: formData.get("path"),
    video_resolution: formData.get("video_resolution"),
    subtitle_language: formData.get("subtitle_language"),
  };
}

function rememberCourseInput() {
  const value = courseInput.value.trim();
  if (value) {
    localStorage.setItem(COURSE_INPUT_STORAGE_KEY, value);
  } else {
    localStorage.removeItem(COURSE_INPUT_STORAGE_KEY);
  }
}

function updateClearButtonVisibility() {
  if (!courseClearBtn) return;
  courseClearBtn.hidden = !courseInput.value;
}

function clearCourseInput() {
  courseInput.value = "";
  localStorage.removeItem(COURSE_INPUT_STORAGE_KEY);
  hideCourseHistory();
  downloadedActivityGroups = [];
  lastSessionLogGroups = [];
  currentTreeNodes = [];
  currentSidebarNodes = [];
  selectedTreeId = "";
  canResumeCurrentCourse = false;
  if (activityTree) activityTree.textContent = "";
  if (logs) logs.innerHTML = "";
  updateClearButtonVisibility();
  refreshDownloadButton();
  showRecentCoursesLanding();
  courseInput.focus();
}

function showRecentCoursesLanding() {
  if (!recentCoursesSection || !activityBody) return;
  if (courseInput.value.trim() || downloadedActivityGroups.length) {
    recentCoursesSection.hidden = true;
    activityBody.hidden = false;
    return;
  }
  const list = getCourseHistory();
  const empty = document.querySelector("#recentCoursesEmpty");
  recentCoursesGrid.innerHTML = list.map((url) => `
    <div class="recent-course-card" role="button" tabindex="0" data-recent-pick="${escapeHtml(url)}" title="${escapeHtml(url)}">
      <span class="recent-course-icon">🎓</span>
      <span class="recent-course-title">${escapeHtml(courseTitleFromUrl(url))}</span>
      <span class="recent-course-url">${escapeHtml(url)}</span>
      <button type="button" class="recent-course-remove" data-history-remove="${escapeHtml(url)}" aria-label="Remove" title="Remove from history">×</button>
    </div>
  `).join("");
  if (empty) empty.hidden = list.length > 0;
  recentCoursesSection.hidden = false;
  activityBody.hidden = true;
}

function hideRecentCoursesLanding() {
  if (recentCoursesSection) recentCoursesSection.hidden = true;
  if (activityBody) activityBody.hidden = false;
}

function courseTitleFromUrl(url) {
  try {
    const u = new URL(url);
    const parts = u.pathname.split("/").filter(Boolean);
    const last = parts[parts.length - 1] || u.hostname;
    return last.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  } catch (error) {
    return url;
  }
}

const COURSE_HISTORY_KEY = "courseraDownloader.courseHistory.v1";
const COURSE_HISTORY_PAGE_SIZE = 5;
const COURSE_HISTORY_MAX = 50;
let courseHistoryPage = 0;
let courseHistorySuppressOpen = false;

function getCourseHistory() {
  try {
    const raw = localStorage.getItem(COURSE_HISTORY_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((item) => typeof item === "string" && item) : [];
  } catch (error) {
    return [];
  }
}

function setCourseHistory(list) {
  localStorage.setItem(COURSE_HISTORY_KEY, JSON.stringify(list.slice(0, COURSE_HISTORY_MAX)));
}

function addToCourseHistory(value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) return;
  const list = getCourseHistory().filter((item) => item !== trimmed);
  list.unshift(trimmed);
  setCourseHistory(list);
}

function removeFromCourseHistory(value) {
  setCourseHistory(getCourseHistory().filter((item) => item !== value));
}

function filteredCourseHistory() {
  const list = getCourseHistory();
  const filter = courseInput.value.trim().toLowerCase();
  if (!filter) return list;
  return list.filter((item) => item.toLowerCase().includes(filter) && item.toLowerCase() !== filter);
}

function renderCourseHistory() {
  const dropdown = document.querySelector("#courseHistory");
  if (!dropdown) return;
  const list = filteredCourseHistory();
  if (!list.length) {
    dropdown.hidden = true;
    dropdown.innerHTML = "";
    return;
  }
  const totalPages = Math.max(1, Math.ceil(list.length / COURSE_HISTORY_PAGE_SIZE));
  if (courseHistoryPage >= totalPages) courseHistoryPage = 0;
  const start = courseHistoryPage * COURSE_HISTORY_PAGE_SIZE;
  const pageItems = list.slice(start, start + COURSE_HISTORY_PAGE_SIZE);
  const showPagination = list.length > COURSE_HISTORY_PAGE_SIZE;
  dropdown.innerHTML = `
    ${pageItems.map((url) => `
      <li class="course-history-item">
        <button type="button" class="course-history-pick" data-history-pick="${escapeHtml(url)}" title="${escapeHtml(url)}">${escapeHtml(url)}</button>
        <button type="button" class="course-history-remove" data-history-remove="${escapeHtml(url)}" title="Remove from history" aria-label="Remove">×</button>
      </li>
    `).join("")}
    ${showPagination ? `
      <li class="course-history-pagination">
        <button type="button" data-history-prev ${courseHistoryPage === 0 ? "disabled" : ""}>‹ Prev</button>
        <span>Page ${courseHistoryPage + 1} / ${totalPages}</span>
        <button type="button" data-history-next ${courseHistoryPage >= totalPages - 1 ? "disabled" : ""}>Next ›</button>
      </li>
    ` : ""}
  `;
  dropdown.hidden = false;
}

function showCourseHistory() {
  if (courseHistorySuppressOpen) return;
  courseHistoryPage = 0;
  renderCourseHistory();
}

function hideCourseHistory() {
  const dropdown = document.querySelector("#courseHistory");
  if (dropdown) {
    dropdown.hidden = true;
    dropdown.innerHTML = "";
  }
}

function refreshDownloadButton() {
  const state = currentStatus.status || "idle";
  const running = Boolean(currentStatus.running);
  const paused = state === "paused";
  const canceling = state === "canceling";

  const completed = isCurrentCourseCompleted();
  downloadBtn.disabled = canceling;
  downloadBtn.textContent = canceling
    ? "Canceling"
    : paused
      ? "Resume"
      : running
        ? "Pause"
        : completed
          ? "Preview"
          : canResumeCurrentCourse
            ? "Resume"
            : "Download";
  downloadBtn.classList.toggle("preview-mode", !running && !canceling && completed);

  pauseBtn.hidden = true;
  cancelBtn.hidden = !running;
  cancelBtn.disabled = canceling;
}

function isCurrentCourseCompleted() {
  if (!downloadedActivityGroups.length) return false;
  const completedStatuses = new Set(["downloaded", "completed"]);
  return downloadedActivityGroups.every((group) => completedStatuses.has(group.status));
}

function previewFolderPath() {
  if (!downloadedActivityGroups.length) return "";
  const first = downloadedActivityGroups[0];
  const firstPath = first && first.path ? String(first.path) : "";
  if (!firstPath) return "";
  if (downloadedActivityGroups.length === 1) {
    return firstPath;
  }
  return firstPath.replace(/[\\/][^\\/]+[\\/]?$/, "") || firstPath;
}

function setRunningState(running, state = "idle") {
  currentStatus = { ...currentStatus, running, status: state };
  refreshDownloadButton();
}

function setSettingsVisible(visible, remember = false) {
  settingsPanel.hidden = !visible;
  settingsToggleBtn.classList.toggle("active", visible);
  settingsToggleBtn.setAttribute("aria-expanded", String(visible));
  if (remember) {
    localStorage.setItem(SETTINGS_STORAGE_KEY, visible ? "1" : "0");
  }
}

function restoreSettingsVisible() {
  const stored = localStorage.getItem(SETTINGS_STORAGE_KEY);
  if (stored === "1" || stored === "0") {
    setSettingsVisible(stored === "1");
    return true;
  }
  return false;
}

function setAuthMessage(text, ok = null, userName = "") {
  loginCheckBtn.textContent = authButtonLabel(text, ok, userName);
  loginCheckBtn.classList.toggle("ok", ok === true);
  loginCheckBtn.classList.toggle("bad", ok === false);
}

function cleanAuthMessage(text) {
  return String(text || "")
    .replace(/\s*detected from\s+[^.]+\.?/gi, "")
    .replace(/\s+/g, " ")
    .trim();
}

function cleanDisplayMessage(text) {
  const message = cleanAuthMessage(text);
  if (/could not auto-detect your coursera login/i.test(message)) {
    return "Could not auto-detect your Coursera login. Start the app with start_web_app.bat, then click Login again.";
  }
  return message;
}

function isAuthDetectionFailure(message) {
  return /could not auto-detect your coursera login/i.test(message || "");
}

function authButtonLabel(text, ok = null, userName = "") {
  const name = String(userName || "").trim();
  if (ok === true && name) {
    return `Welcome, ${name}`;
  }

  const message = cleanAuthMessage(text);
  const loggedInMatch = message.match(/^logged in as\s+(.+?)\.?$/i);
  if (ok === true && loggedInMatch && loggedInMatch[1].trim()) {
    return `Welcome, ${loggedInMatch[1].trim()}`;
  }

  if (ok === true && /^logged in\.?$/i.test(message)) {
    return "Welcome";
  }

  return message || "Login";
}

function rememberAuth(result) {
  if (!result.authenticated) return;
  const message = cleanAuthMessage(result.message);
  localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify({
    message,
    userName: result.user_name || "",
    savedAt: Date.now(),
  }));
}

function restoreAuth() {
  try {
    const stored = JSON.parse(localStorage.getItem(AUTH_STORAGE_KEY) || "null");
    if (stored && stored.message) {
      const message = cleanAuthMessage(stored.message);
      if (message !== stored.message) {
        localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify({ ...stored, message }));
      }
      setAuthMessage(message, true, stored.userName);
      loginOpened = true;
      return true;
    }
  } catch {
    localStorage.removeItem(AUTH_STORAGE_KEY);
  }
  return false;
}

function renderStatus(status) {
  const state = status.status || "idle";
  jobState.textContent = state.charAt(0).toUpperCase() + state.slice(1);
  jobState.className = `status-pill ${state}`;
  setRunningState(Boolean(status.running), state);

  if (status.logs && status.logs.length) {
    renderGroupedLogs(status.logs);
    logs.scrollTop = logs.scrollHeight;
  } else if (status.error) {
    logs.textContent = status.error;
    activityTree.textContent = "Error";
  }

  if (status.running && treeNeedsStateRender()) {
    void updateDownloadState({ force: true, render: true });
  }

  if (status.error) {
    showMessage(status.error);
  }
}

function slugId(value) {
  return String(value || "item").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[char]));
}

function stripLogPrefix(line) {
  return line.replace(/^\[[^\]]+\]\s*/, "").replace(/^INFO:\s*/, "");
}

function createGroup(title, slug = "") {
  return { title, slug, modules: [], loose: [], logs: [], index: 0, total: 0, status: "running" };
}

function createModule(title, slug = "") {
  return { title, slug, sections: [], loose: [], logs: [] };
}

function createSection(title, slug = "") {
  return { title, slug, items: [], logs: [] };
}

function normalizeName(value) {
  return value.replace(/_/g, " ").replace(/-/g, " ").replace(/\s+/g, " ").trim();
}

function parsePathActivity(message) {
  const match =
    message.match(/Downloading:\s+(.*)$/) ||
    message.match(/Saving page contents to:\s+(.*)$/) ||
    message.match(/^(.*)\s+already downloaded$/);
  if (!match) return null;
  const path = match[1].trim().replace(/^\\\\\?\\/, "");
  const parts = path.split(/[\\/]+/).filter(Boolean);
  return {
    text: parts.slice(-4).join(" / "),
    path,
    parts,
  };
}

function titleForCourseSlug(slug) {
  const downloaded = downloadedActivityGroups.find((group) => group.slug === slug);
  return downloaded?.title || normalizeName(slug);
}

function inferCourseSlug(parts) {
  const slugs = downloadedActivityGroups.map((group) => group.slug).filter(Boolean);
  return slugs.find((slug) => parts.includes(slug)) || "";
}

function findOrCreateModule(course, title, slug) {
  let found = course.modules.find((item) => item.slug === slug || item.title === title);
  if (!found) {
    found = createModule(title, slug);
    course.modules.push(found);
  }
  return found;
}

function findOrCreateSection(module, title, slug) {
  let found = module.sections.find((item) => item.slug === slug || item.title === title);
  if (!found) {
    found = createSection(title, slug);
    module.sections.push(found);
  }
  return found;
}

function buildActivityGroups(lines) {
  const groups = [];
  let course = createGroup("Session");
  let module = createModule("General");
  let section = createSection("General");
  course.modules.push(module);
  module.sections.push(section);
  groups.push(course);

  for (const raw of lines) {
    const message = stripLogPrefix(raw);
    if (!message || message.includes('127.0.0.1 - "')) continue;

    let match = message.match(/Downloading class:\s+(.+?)\s+\((\d+)\s+\/\s+(\d+)\)/);
    if (match) {
      if (course.title !== "Session") {
        course.status = "completed";
      }
      course = createGroup(`${normalizeName(match[1])} (${match[2]}/${match[3]})`, match[1]);
      course.index = Number(match[2]);
      course.total = Number(match[3]);
      course.is_active = true;
      course.logs.push(message);
      module = createModule("General");
      section = createSection("General");
      course.modules.push(module);
      module.sections.push(section);
      groups.push(course);
      continue;
    }

    match = message.match(/Processing module\s+(.+)$/);
    if (match) {
      module = createModule(normalizeName(match[1]), match[1]);
      course.is_active = true;
      module.is_active = true;
      course.logs.push(message);
      module.logs.push(message);
      section = createSection("General");
      module.sections.push(section);
      course.modules.push(module);
      continue;
    }

    match = message.match(/Processing section\s+(.+)$/);
    if (match) {
      section = createSection(normalizeName(match[1]), match[1]);
      course.is_active = true;
      module.is_active = true;
      section.is_active = true;
      course.logs.push(message);
      module.logs.push(message);
      section.logs.push(message);
      module.sections.push(section);
      continue;
    }

    match = message.match(/Processing lecture\s+(.+)$/);
    if (match) {
      section.items.push({ kind: "lecture", text: normalizeName(match[1]), slug: match[1] });
      course.is_active = true;
      module.is_active = true;
      section.is_active = true;
      course.logs.push(message);
      module.logs.push(message);
      section.logs.push(message);
      continue;
    }

    const pathDownload = parsePathActivity(message);
    if (pathDownload) {
      const inferredSlug = inferCourseSlug(pathDownload.parts);
      if (inferredSlug && course.slug !== inferredSlug) {
        if (course.title !== "Session") {
          course.status = "completed";
        }
        course = createGroup(titleForCourseSlug(inferredSlug), inferredSlug);
        groups.push(course);
      }
      const courseIndex = inferredSlug ? pathDownload.parts.indexOf(inferredSlug) : -1;
      const moduleSlug = courseIndex >= 0 ? pathDownload.parts[courseIndex + 1] : "";
      const sectionSlug = courseIndex >= 0 ? pathDownload.parts[courseIndex + 2] : "";
      if (moduleSlug) {
        module = findOrCreateModule(course, normalizeName(moduleSlug), moduleSlug);
      }
      if (sectionSlug) {
        section = findOrCreateSection(module, normalizeName(sectionSlug), sectionSlug);
      }
      course.status = "running";
      course.is_active = true;
      course.logs.push(message);
      module.is_active = true;
      module.logs.push(message);
      section.is_active = true;
      section.items.push({ kind: "file", text: pathDownload.text, path: pathDownload.path, parts: pathDownload.parts, log: message, active: true });
      section.logs.push(message);
      continue;
    }

    if (
      message.includes("Download started") ||
      message.includes("Resume started") ||
      message.includes("Download completed") ||
      message.includes("Expanded specialization") ||
      message.includes("Logged in")
    ) {
      course.loose.push(message);
      course.logs.push(message);
      continue;
    }

    if (message.includes("Unsupported typename") || message.includes("skipped")) {
      section.items.push({ kind: "note", text: message });
      section.logs.push(message);
      continue;
    }
  }

  if (lines.some((line) => stripLogPrefix(line).includes("Download completed"))) {
    course.status = "completed";
  }

  return groups.filter((group) =>
    group.loose.length || group.modules.some((item) =>
      item.loose.length || item.sections.some((sectionItem) => sectionItem.items.length)
    )
  );
}

function renderGroupedLogs(lines) {
  const groups = buildActivityGroups(lines);
  const hasRealCourseLogs = groups.some((group) => group.slug || group.title !== "Session");
  console.debug("[tree-debug] renderGroupedLogs", {
    logLines: lines.length,
    parsedGroups: summarizeActivityGroups(groups),
    downloadedGroups: summarizeActivityGroups(downloadedActivityGroups),
    hasRealCourseLogs,
  });
  if (!downloadedActivityGroups.length) {
    lastSessionLogGroups = groups;
    currentTreeNodes = [];
    currentSidebarNodes = [];
    activityTree.textContent = "Waiting for site tree manifest.";
    logs.innerHTML = statusMessagesHtml() + renderSessionLogGroups(groups);
    return;
  }
  if (!hasRealCourseLogs) {
    lastSessionLogGroups = groups;
    renderActivityGroups(downloadedActivityGroups);
    return;
  }
  renderActivityGroups(hasRealCourseLogs ? mergeDownloadedActivityGroups(groups) : groups, { preferActive: currentStatus.running });
}

function renderSessionLogGroups(groups) {
  const sessionNode = buildFolderTree(groups)[0];
  if (!sessionNode) return "";
  return `
    <section class="folder-detail">
      <div class="folder-detail-head">
        <div>
          <h3>Session</h3>
          <p>Download logs</p>
        </div>
      </div>
      ${renderNodeLogs(sessionNode)}
    </section>
  `;
}

function mergeDownloadedActivityGroups(groups) {
  if (!downloadedActivityGroups.length) return groups;
  const merged = downloadedActivityGroups.map((group) => ({ ...group }));
  groups.forEach((group) => {
    const existingIndex = group.slug ? merged.findIndex((item) => item.slug === group.slug) : -1;
    if (existingIndex >= 0) {
      const existing = merged[existingIndex];
      merged[existingIndex] = {
        ...existing,
        status: group.status || existing.status,
        logs: mergeUnique(existing.logs || [], group.logs || []),
        loose: mergeUnique(existing.loose || [], group.loose || []),
        is_active: Boolean(group.is_active) || Boolean(existing.is_active),
        index: group.index || existing.index,
        total: group.total || existing.total,
      };
    } else if (!group.slug || !merged.some((item) => item.slug === group.slug)) {
      merged.push(group);
    }
  });
  console.debug("[tree-debug] mergeDownloadedActivityGroups", {
    liveGroups: summarizeActivityGroups(groups),
    mergedGroups: summarizeActivityGroups(merged),
  });
  return merged;
}

function mergeUnique(first, second) {
  return [...first, ...second].filter((item, index, items) => items.indexOf(item) === index);
}

function summarizeActivityGroups(groups) {
  return (groups || []).map((group) => ({
    slug: group.slug || "",
    title: group.title || "",
    modules: (group.modules || []).length,
    sections: (group.modules || []).reduce((total, module) => total + (module.sections || []).length, 0),
    fileCount: group.file_count,
    downloadableCount: group.downloadable_count,
    expectedCount: group.expected_count,
    status: group.status || "",
    active: Boolean(group.is_active),
  }));
}

function renderActivityGroups(groups, options = {}) {
  if (!groups.length) {
    logs.innerHTML = statusMessagesHtml() || "";
    activityTree.textContent = "";
    currentTreeNodes = [];
    currentSidebarNodes = [];
    return;
  }
  hideRecentCoursesLanding();

  currentTreeNodes = buildFolderTree(groups);
  currentSidebarNodes = currentTreeNodes.filter((node) => !isSessionNode(node));
  if (!userChangedExpansion && !autoExpandedInitialTree && currentSidebarNodes.length) {
    expandAllTreeNodes(currentSidebarNodes);
    autoExpandedInitialTree = true;
  }
  activeTreeId = options.preferActive ? findActiveTreeNode(currentSidebarNodes)?.id || "" : "";
  const existingSelection = findTreeNode(currentSidebarNodes, selectedTreeId)?.id;
  selectedTreeId =
    existingSelection ||
    activeTreeId ||
    deepestActiveNode(currentSidebarNodes)?.id ||
    findTreeNode(currentTreeNodes, selectedTreeId)?.id ||
    deepestActiveNode(currentTreeNodes)?.id ||
    currentTreeNodes[0].id;
  ensureSelectedTreeExpanded();
  activityTree.innerHTML = currentSidebarNodes.length
    ? renderTreeNodes(currentSidebarNodes)
    : "";
  console.debug("[tree-debug] renderActivityGroups", {
    sourceGroups: summarizeActivityGroups(groups),
    sidebarNodes: summarizeTreeNodes(currentSidebarNodes),
    selectedNode: summarizeTreeNode(findTreeNode(currentTreeNodes, selectedTreeId)),
    selectedNodeChildren: (findTreeNode(currentTreeNodes, selectedTreeId)?.children || []).map(summarizeTreeNode),
    roots: currentSidebarNodes.map((node) => ({
      id: node.id,
      title: node.title,
      children: node.children.length,
      files: nodeFileCount(node),
      expected: node.expectedCount,
      active: node.active,
    })),
    selectedTreeId,
    activeTreeId,
    expandedCount: expandedTreeIds.size,
  });
  renderSelectedTreeNode();
}

function summarizeTreeNodes(nodes) {
  return (nodes || []).map(summarizeTreeNode);
}

function summarizeTreeNode(node) {
  if (!node) return null;
  return {
    id: node.id,
    title: node.title,
    type: node.type,
    children: node.children.length,
    files: nodeFileCount(node),
    expected: node.expectedCount,
    expanded: expandedTreeIds.has(node.id),
    active: node.active,
  };
}

function buildFolderTree(groups) {
  return groups.map((group, groupIndex) => {
    const node = makeTreeNode(`course-${groupIndex}-${slugId(group.slug || group.title)}`, group.title, "course", {
      status: group.status,
      slug: group.slug,
      logs: [...(group.logs || []), ...(group.loose || [])],
      fileCount: group.file_count,
      downloadableCount: group.downloadable_count,
      expectedCount: group.expected_count,
      index: group.index,
      total: group.total,
      orderIndex: group.order_index,
      orderTotal: group.order_total,
      active: group.is_active,
      treeSource: group.tree_source,
    });

    (group.modules || []).forEach((module, moduleIndex) => {
      const moduleNode = makeTreeNode(`${node.id}-folder-${moduleIndex}-${slugId(module.slug || module.title)}`, module.title, "folder", {
        slug: module.slug,
        logs: module.logs || module.loose || [],
        fileCount: module.file_count,
        downloadableCount: module.downloadable_count,
        expectedCount: module.expected_count,
        active: module.is_active,
        treeSource: module.tree_source || group.tree_source,
      });
      node.children.push(moduleNode);

      (module.sections || []).forEach((section, sectionIndex) => {
        const sectionNode = makeTreeNode(`${moduleNode.id}-folder-${sectionIndex}-${slugId(section.slug || section.title)}`, section.title, "folder", {
          slug: section.slug,
          logs: section.logs || [],
          fileCount: section.file_count,
          downloadableCount: section.downloadable_count,
          expectedCount: section.expected_count,
          active: section.is_active,
          treeSource: section.tree_source || module.tree_source || group.tree_source,
          actions: group.slug && section.slug && section.title !== "General"
            ? { course: group.slug, section: section.slug }
            : null,
        });
        moduleNode.children.push(sectionNode);
        (section.items || []).forEach((item, itemIndex) => addItemToTree(sectionNode, item, itemIndex));
      });
    });

    return node;
  });
}

function makeTreeNode(id, title, type, options = {}) {
  return {
    id,
    title,
    type,
    status: options.status || "",
    slug: options.slug || "",
    logs: options.logs || [],
    children: [],
    files: [],
    fileCount: options.fileCount,
    downloadableCount: options.downloadableCount,
    expectedCount: options.expectedCount,
    index: options.index || 0,
    total: options.total || 0,
    orderIndex: options.orderIndex || 0,
    orderTotal: options.orderTotal || 0,
    active: Boolean(options.active),
    treeSource: options.treeSource || "",
    actions: options.actions || null,
  };
}

function addItemToTree(parent, item, itemIndex) {
  if (item.kind === "file" || item.kind === "video" || item.kind === "text" || item.kind === "resource") {
    const parts = item.parts || String(item.path || item.text || "").split(/[\\/]+/).filter(Boolean);
    const usefulParts = trimFileParts(parts, item.text);
    let node = parent;
    usefulParts.slice(0, -1).forEach((part, depth) => {
      const childId = `${node.id}-path-${depth}-${slugId(part)}`;
      let child = node.children.find((candidate) => candidate.id === childId);
      if (!child) {
        child = makeTreeNode(childId, normalizeName(part), "folder");
        node.children.push(child);
      }
      if (item.log && !child.logs.includes(item.log)) child.logs.push(item.log);
      if (item.active) child.active = true;
      node = child;
    });
    if (item.log && !node.logs.includes(item.log)) node.logs.push(item.log);
    if (item.active) node.active = true;
    node.files.push({ ...item, text: usefulParts[usefulParts.length - 1] || item.text || "File" });
    return;
  }
  parent.files.push({ ...item, text: item.text || `Item ${itemIndex + 1}` });
}

function trimFileParts(parts, fallbackText) {
  const fallbackParts = String(fallbackText || "").split(/\s+\/\s+|[\\/]+/).filter(Boolean);
  if (fallbackParts.length > 1) return fallbackParts;
  const fileIndex = parts.findLastIndex((part) => /\.[a-z0-9]{1,8}$/i.test(part));
  const useful = fileIndex >= 0 ? parts.slice(Math.max(0, fileIndex - 5), fileIndex + 1) : parts.slice(-1);
  return useful.length ? useful : fallbackParts;
}

function renderTreeNodes(nodes, depth = 0) {
  return nodes.map((node) => {
    const isOpen = expandedTreeIds.has(node.id);
    const hasChildren = node.children.length > 0;
    const badge = node.type === "course" && node.status
      ? `<span class="tree-status-badge ${escapeHtml(node.status)}">${escapeHtml(statusText(node.status))}</span>`
      : "";
    return `
      <button type="button" class="tree-node ${node.type} ${node.id === selectedTreeId ? "active" : ""}" style="--depth:${depth}" data-node-id="${escapeHtml(node.id)}">
        <span class="tree-toggle" data-toggle-node="${escapeHtml(node.id)}">${hasChildren ? (isOpen ? "▾" : "▸") : ""}</span>
        <span class="tree-label">${escapeHtml(node.title)}</span>
        ${badge}
        <span class="tree-meta">${escapeHtml(nodeProgressText(node))}</span>
      </button>
      ${hasChildren && isOpen ? renderTreeNodes(node.children, depth + 1) : ""}
    `;
  }).join("");
}

function renderSelectedTreeNode() {
  const node = findTreeNode(currentTreeNodes, selectedTreeId) || currentTreeNodes[0];
  if (!node) {
    if (lastSessionLogGroups.length) {
      const sessionNode = buildFolderTree(lastSessionLogGroups)[0];
      logs.innerHTML = statusMessagesHtml() + `
        <section class="folder-detail">
          <div class="folder-detail-head">
            <div>
              <h3>Session</h3>
              <p>Startup logs</p>
            </div>
          </div>
          ${renderNodeLogs(sessionNode)}
        </section>
      `;
      return;
    }
    logs.innerHTML = statusMessagesHtml() || "No download has started.";
    return;
  }

  ensureSelectedTreeExpanded();
  activityTree.innerHTML = currentSidebarNodes.length
    ? renderTreeNodes(currentSidebarNodes)
    : "";
  const percent = node.total ? Math.round((Math.max(node.index - 1, 0) / node.total) * 100) : 0;
  const showLogs = selectedTreeView === "logs" || node.id === activeTreeId || (!node.files.length && !node.children.length && node.logs.length);
  logs.innerHTML = statusMessagesHtml() + `
    <section class="folder-detail">
      <div class="folder-detail-head">
        <div>
          <h3>${escapeHtml(node.title)}</h3>
          <p>${escapeHtml(detailSummary(node))}</p>
        </div>
        <div class="activity-actions">
          ${node.slug && node.type === "course" ? `
            <button type="button" data-action="resume" data-type="course" data-course="${escapeHtml(node.slug)}">Resume</button>
            <button type="button" data-action="retry" data-type="course" data-course="${escapeHtml(node.slug)}">Retry</button>
          ` : ""}
          ${node.actions ? `
            <button type="button" data-action="resume" data-type="section" data-course="${escapeHtml(node.actions.course)}" data-section="${escapeHtml(node.actions.section)}">Resume</button>
            <button type="button" data-action="retry" data-type="section" data-course="${escapeHtml(node.actions.course)}" data-section="${escapeHtml(node.actions.section)}">Retry</button>
          ` : ""}
          <button type="button" class="secondary small" data-view="${showLogs ? "content" : "logs"}">${showLogs ? "Show content" : "Show logs"}</button>
        </div>
      </div>
      ${node.total ? `<div class="progress"><i style="width:${percent}%"></i></div>` : ""}
      ${showLogs ? renderNodeLogs(node) : renderNodeContent(node)}
    </section>
  `;
}

function renderNodeContent(node) {
  const children = node.children.map((child) => `
    <button type="button" class="folder-row" data-open-node="${escapeHtml(child.id)}">
      <span>folder</span>
      <p>${escapeHtml(child.title)}</p>
      <em>${escapeHtml(nodeProgressText(child))}</em>
    </button>
  `).join("");
  const files = node.files.map((item) => `
    <div class="activity-item ${escapeHtml(item.kind || "file")}${item.downloadable === false ? " non-downloadable" : ""}">
      <span>${escapeHtml(itemKindLabel(item.kind || "file"))}</span>
      <p>${renderItemSourceLink(item)}</p>
      <em class="item-status ${escapeHtml(item.status || "")}" title="${escapeHtml(item.reason || "")}">${escapeHtml(itemStatusText(item))}</em>
    </div>
  `).join("");
  return children || files ? children + files : renderNodeLogs(node);
}

function renderItemSourceLink(item) {
  const source = preferredItemSource(item);
  if (!source || !source.path) return escapeHtml(item.text);
  const extra = item.sources && item.sources.length > 1 ? ` (${item.sources.length} files)` : "";
  return `
    <button type="button" class="source-link" data-open-path="${escapeHtml(source.path)}" title="${escapeHtml(source.path)}">
      ${escapeHtml(item.text)}${escapeHtml(extra)}
    </button>
  `;
}

function preferredItemSource(item) {
  const sources = Array.isArray(item.sources) ? item.sources : [];
  return sources.find((source) => source.path) || null;
}

function itemKindLabel(kind) {
  const labels = {
    lecture: "video",
    supplement: "reading",
    quiz: "assignment",
    exam: "assignment",
    phasedPeer: "assignment",
    staffGraded: "assignment",
    ungradedAssignment: "assignment",
    discussionPrompt: "discussion",
    ungradedWidget: "plugin",
    coach: "plugin",
  };
  return labels[kind] || kind || "file";
}

function itemStatusText(item) {
  if (!item.status) return "";
  if (item.status === "downloaded") return "Downloaded";
  if (item.status === "partial") return `Partial ${item.downloaded_count || 0}/${item.expected_count || "?"}`;
  if (item.status === "missing") return "Missing";
  if (item.status === "not-downloadable") return item.reason ? "Not downloadable" : "Info only";
  if (item.status === "pending") return "Checking…";
  return item.status;
}

function renderNodeLogs(node) {
  const logLines = node.logs.length ? node.logs : ["No files yet. New download logs for this folder will appear here."];
  return logLines.map((item) => `<div class="activity-note">${escapeHtml(item)}</div>`).join("");
}

function detailSummary(node) {
  const files = nodeFileCount(node);
  const folders = node.children.length;
  const status = node.status ? statusText(node.status, node.fileCount) : "";
  const source = node.treeSource === "site" ? "Site tree" : node.treeSource === "missing" ? "Manifest missing" : "";
  const unit = node.treeSource === "site" ? "item" : "file";
  return [source, status, folders ? `${folders} folder${folders === 1 ? "" : "s"}` : "", nodeProgressText(node), `${files} ${unit}${files === 1 ? "" : "s"}`]
    .filter(Boolean)
    .join(" · ");
}

function nodeProgressText(node) {
  if (node.type === "course" && (typeof node.expectedCount === "number" || typeof node.fileCount === "number")) {
    const label = itemProgressText(node);
    if (label) return label;
  }
  if (node.total) {
    return `${Math.max(node.index - 1, 0)} done from ${node.total}`;
  }
  const label = itemProgressText(node);
  if (label) return label;
  if (node.status) return statusText(node.status, node.fileCount);
  return "0 done from 0";
}

function itemProgressText(node) {
  const done = nodeFileCount(node);
  const downloadable = typeof node.downloadableCount === "number" && node.downloadableCount > 0
    ? node.downloadableCount
    : 0;
  const expected = typeof node.expectedCount === "number" && node.expectedCount > 0
    ? node.expectedCount
    : 0;
  const total = downloadable || expected || (typeof node.fileCount === "number" ? node.fileCount : done);
  if (!total && !done) return "";
  if (downloadable && expected && downloadable !== expected) {
    return `${done} done from ${downloadable} (${expected})`;
  }
  return `${done} done from ${total}`;
}

function nodeFileCount(node) {
  if (typeof node.fileCount === "number") return node.fileCount;
  return node.files.length + node.children.reduce((total, child) => total + nodeFileCount(child), 0);
}

function findTreeNode(nodes, id) {
  for (const node of nodes) {
    if (node.id === id) return node;
    const child = findTreeNode(node.children, id);
    if (child) return child;
  }
  return null;
}

function deepestActiveNode(nodes) {
  const flat = [];
  const visit = (node) => {
    flat.push(node);
    node.children.forEach(visit);
  };
  nodes.forEach(visit);
  return [...flat].reverse().find((node) => node.logs.length || node.files.length) || flat[flat.length - 1];
}

function findActiveTreeNode(nodes) {
  for (const node of nodes) {
    if (node.active) {
      const child = findActiveTreeNode(node.children);
      return child || node;
    }
    const child = findActiveTreeNode(node.children);
    if (child) return child;
  }
  return null;
}

function isSessionNode(node) {
  return node.title === "Session" && !node.slug;
}

function ensureSelectedTreeExpanded() {
  const path = findTreePath(currentSidebarNodes, selectedTreeId);
  if (!path.length) return;
  path.slice(0, -1).forEach((node) => expandedTreeIds.add(node.id));
}

function expandAllTreeNodes(nodes = currentSidebarNodes) {
  const visit = (node) => {
    if (node.children.length) {
      expandedTreeIds.add(node.id);
      node.children.forEach(visit);
    }
  };
  nodes.forEach(visit);
}

function collapseAllTreeNodes() {
  expandedTreeIds.clear();
}

function findTreePath(nodes, id, path = []) {
  for (const node of nodes) {
    const nextPath = [...path, node];
    if (node.id === id) return nextPath;
    const childPath = findTreePath(node.children, id, nextPath);
    if (childPath.length) return childPath;
  }
  return [];
}

function statusText(status, fileCount = null) {
  if (status === "completed") return "Completed";
  if (status === "downloaded") return "Completed";
  if (status === "partial") return fileCount ? `Partial · ${fileCount}` : "Partial";
  if (status === "missing") return "Not started";
  if (status === "paused") return "Paused";
  if (status === "canceling") return "Canceling";
  if (status === "canceled") return "Canceled";
  if (status === "failed") return "Failed";
  if (status === "manifest-missing") return "Manifest missing";
  if (status === "manifest") return "Loading…";
  if (status === "pending") return "Loading…";
  if (status === "running") return "Running";
  return "Running";
}

async function refreshStatus() {
  const status = await requestJson("/api/status");
  renderStatus(status);
  if (status.running) {
    await updateDownloadState({ force: true, render: treeNeedsStateRender() });
  }
  if (!status.running && statusTimer) {
    clearInterval(statusTimer);
    statusTimer = null;
    await updateDownloadState({ render: !(status.logs && status.logs.length) });
  }
}

async function startDownload(endpoint) {
  clearMessage();
  userChangedExpansion = false;
  autoExpandedInitialTree = false;
  expandedTreeIds.clear();
  const result = await requestJson(endpoint, {
    method: "POST",
    body: JSON.stringify(payloadFromForm()),
  });
  rememberCourseInput();
  renderStatus(result.status);
  showMessage(`${endpoint === "/api/resume" ? "Resume" : "Download"} started. Keep this server window open until it finishes.`, true);
  if (!statusTimer) {
    statusTimer = setInterval(refreshStatus, 1500);
  }
  await updateDownloadState({ force: true });
}

async function updateDownloadState(options = {}) {
  if (downloadStatePromise) {
    return downloadStatePromise;
  }
  downloadStatePromise = performDownloadStateUpdate(options)
    .finally(() => {
      downloadStatePromise = null;
    });
  return downloadStatePromise;
}

async function performDownloadStateUpdate(options = {}) {
  const shouldRender = options.render !== false;
  if (currentStatus.running && !options.force) return;
  const now = Date.now();
  if (
    currentStatus.running &&
    downloadedActivityGroups.length &&
    now - lastDownloadStateAt < 10000
  ) {
    if (treeNeedsStateRender()) {
      renderActivityGroups(downloadedActivityGroups);
    }
    return;
  }
  try {
    lastDownloadStateAt = now;
    if (!downloadedActivityGroups.length) {
      try {
        const quickState = await requestJson("/api/download-state", {
          method: "POST",
          body: JSON.stringify({ ...payloadFromForm(), quick: true }),
        });
        if (Array.isArray(quickState.activity_groups) && quickState.activity_groups.length) {
          downloadedActivityGroups = quickState.activity_groups;
          if ((!currentStatus.running && shouldRender) || treeNeedsStateRender()) {
            renderActivityGroups(quickState.activity_groups);
          }
        }
      } catch (quickError) {
        console.debug("[tree-debug] quick state failed", quickError);
      }
    }
    const state = await requestJson("/api/download-state", {
      method: "POST",
      body: JSON.stringify(payloadFromForm()),
    });
    canResumeCurrentCourse = Boolean(state.can_resume);
    if (state.resume_path && pathInput.value !== state.resume_path) {
      pathInput.value = state.resume_path;
    }
    if (Array.isArray(state.activity_groups) && state.activity_groups.length) {
      downloadedActivityGroups = state.activity_groups;
      console.debug("[tree-debug] updateDownloadState", {
        canResume: state.can_resume,
        className: state.class_name,
        courseFolder: state.course_folder,
        groups: summarizeActivityGroups(state.activity_groups),
        running: currentStatus.running,
        shouldRender,
      });
      const treeIsPlaceholder = treeNeedsStateRender();
      if ((!currentStatus.running && shouldRender) || treeIsPlaceholder) {
        renderActivityGroups(state.activity_groups);
      }
    }
  } catch (error) {
    console.debug("[tree-debug] updateDownloadState failed", error);
    canResumeCurrentCourse = false;
  }
  refreshDownloadButton();
}

async function startItemDownload(action, item) {
  clearMessage();
  const endpoint = action === "resume" ? "/api/resume-item" : "/api/download-item";
  const result = await requestJson(endpoint, {
    method: "POST",
    body: JSON.stringify({ ...payloadFromForm(), item }),
  });
  renderStatus(result.status);
  showMessage(`${action === "resume" ? "Resume" : "Retry"} started for ${item.type}.`, true);
  if (!statusTimer) {
    statusTimer = setInterval(refreshStatus, 1500);
  }
}

async function controlDownload(endpoint, message) {
  clearMessage();
  const result = await requestJson(endpoint, { method: "POST", body: "{}" });
  renderStatus(result.status);
  showMessage(message, true);
  if (!statusTimer && result.status.running) {
    statusTimer = setInterval(refreshStatus, 1500);
  }
}

async function init() {
  const config = await requestJson("/api/config");
  versionLabel.textContent = `Version ${config.version}`;
  if (!restoreAuth()) {
    setAuthMessage("Login");
  }
  fillSelect(languageSelect, config.languages, config.defaults.subtitle_language);
  const lastInput = localStorage.getItem(COURSE_INPUT_STORAGE_KEY) || config.defaults.course || "";
  if (lastInput) {
    addToCourseHistory(lastInput);
  }
  courseInput.value = "";
  updateClearButtonVisibility();
  showRecentCoursesLanding();
  pathInput.value = config.defaults.path || "";
  if (!restoreSettingsVisible()) {
    setSettingsVisible(false, true);
  }

  const resolution = config.defaults.video_resolution || "720p";
  const resolutionInput = document.querySelector(`input[name="video_resolution"][value="${resolution}"]`);
  if (resolutionInput) {
    resolutionInput.checked = true;
  }

  const initialDownloadState = updateDownloadState({ force: true, render: true });
  await refreshStatus();
  await initialDownloadState;
  await checkAuthOnce(false);
}

async function checkAuthOnce(showResult = true) {
  if (!authCheckPromise) {
    authCheckPromise = requestJson("/api/check-auth", { method: "POST", body: "{}" })
      .finally(() => {
        authCheckPromise = null;
      });
  }
  const result = await authCheckPromise;
  setAuthMessage(result.message, result.authenticated, result.user_name);
  if (result.authenticated) {
    rememberAuth(result);
  }
  if (showResult && !result.authenticated && !isAuthDetectionFailure(result.message)) {
    showMessage(result.message, result.authenticated);
  }
  await refreshStatus();
  return result;
}

function startAuthPolling() {
  if (authPollTimer) {
    clearInterval(authPollTimer);
  }

  let attempts = 0;
  authPollTimer = setInterval(async () => {
    attempts += 1;
    try {
      const result = await checkAuthOnce(false);
      if (result.authenticated) {
        clearInterval(authPollTimer);
        authPollTimer = null;
        if (loginWindow && !loginWindow.closed) {
          loginWindow.close();
        }
        loginWindow = null;
      }
    } catch {
      // Keep polling quietly while the user finishes the browser login.
    }

    if (attempts >= 40) {
      clearInterval(authPollTimer);
      authPollTimer = null;
      setAuthMessage("Check Auth", false);
    }
  }, 3000);
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  rememberCourseInput();
  addToCourseHistory(courseInput.value);
  hideCourseHistory();
  try {
    if (currentStatus.running) {
      const paused = currentStatus.status === "paused";
      await controlDownload(
        paused ? "/api/resume-job" : "/api/pause",
        paused ? "Download resumed." : "Download paused."
      );
      return;
    }

    if (isCurrentCourseCompleted()) {
      const path = previewFolderPath();
      if (!path) {
        showMessage("Course folder is not available yet.");
        return;
      }
      await requestJson("/api/open-path", { method: "POST", body: JSON.stringify({ path }) });
      return;
    }

    await startDownload(canResumeCurrentCourse ? "/api/resume" : "/api/download");
  } catch (error) {
    showMessage(error.message);
  }
});

courseInput.addEventListener("input", () => {
  rememberCourseInput();
  courseHistoryPage = 0;
  updateClearButtonVisibility();
  renderCourseHistory();
  if (courseInput.value.trim()) {
    hideRecentCoursesLanding();
  } else if (!downloadedActivityGroups.length) {
    showRecentCoursesLanding();
  }
});

if (courseClearBtn) {
  courseClearBtn.addEventListener("click", (event) => {
    event.preventDefault();
    clearCourseInput();
  });
}

function pickRecentCourse(url) {
  if (!url) return;
  courseInput.value = url;
  rememberCourseInput();
  addToCourseHistory(url);
  updateClearButtonVisibility();
  hideCourseHistory();
  hideRecentCoursesLanding();
  void updateDownloadState({ force: true, render: true });
}

document.addEventListener("click", (event) => {
  const card = event.target.closest("[data-recent-pick]");
  if (!card) return;
  if (event.target.closest("[data-history-remove]")) return;
  event.preventDefault();
  pickRecentCourse(card.dataset.recentPick);
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  const card = event.target.closest("[data-recent-pick]");
  if (!card) return;
  event.preventDefault();
  pickRecentCourse(card.dataset.recentPick);
});

courseInput.addEventListener("focus", showCourseHistory);

courseInput.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    hideCourseHistory();
  }
});

document.addEventListener("mousedown", (event) => {
  const dropdown = document.querySelector("#courseHistory");
  if (!dropdown || dropdown.hidden) return;
  if (event.target === courseInput) return;
  if (dropdown.contains(event.target)) return;
  hideCourseHistory();
});

document.addEventListener("click", (event) => {
  const pickButton = event.target.closest("[data-history-pick]");
  if (pickButton) {
    event.preventDefault();
    courseInput.value = pickButton.dataset.historyPick;
    rememberCourseInput();
    addToCourseHistory(courseInput.value);
    hideCourseHistory();
    courseInput.dispatchEvent(new Event("change", { bubbles: true }));
    void updateDownloadState({ force: true, render: true });
    return;
  }
  const removeButton = event.target.closest("[data-history-remove]");
  if (removeButton) {
    event.preventDefault();
    event.stopPropagation();
    removeFromCourseHistory(removeButton.dataset.historyRemove);
    renderCourseHistory();
    if (recentCoursesSection && !recentCoursesSection.hidden) {
      showRecentCoursesLanding();
    }
    return;
  }
  if (event.target.closest("[data-history-prev]")) {
    event.preventDefault();
    if (courseHistoryPage > 0) courseHistoryPage -= 1;
    renderCourseHistory();
    courseInput.focus();
    return;
  }
  if (event.target.closest("[data-history-next]")) {
    event.preventDefault();
    courseHistoryPage += 1;
    renderCourseHistory();
    courseInput.focus();
    return;
  }
});

refreshBtn.addEventListener("click", async () => {
  try {
    await refreshStatus();
  } catch (error) {
    showMessage(error.message);
  }
});

cancelBtn.addEventListener("click", async () => {
  cancelBtn.disabled = true;
  try {
    await controlDownload("/api/cancel", "Cancel requested. The current download will stop shortly.");
  } catch (error) {
    showMessage(error.message);
  } finally {
    if (!cancelBtn.hidden && downloadBtn.textContent !== "Canceling") {
      cancelBtn.disabled = false;
    }
  }
});

logs.addEventListener("click", async (event) => {
  const sourceButton = event.target.closest("button[data-open-path]");
  if (sourceButton) {
    try {
      await requestJson("/api/open-path", {
        method: "POST",
        body: JSON.stringify({ path: sourceButton.dataset.openPath }),
      });
    } catch (error) {
      showMessage(error.message);
    }
    return;
  }

  const viewButton = event.target.closest("button[data-view]");
  if (viewButton) {
    selectedTreeView = viewButton.dataset.view;
    renderSelectedTreeNode();
    return;
  }

  const folderButton = event.target.closest("button[data-open-node]");
  if (folderButton) {
    selectedTreeId = folderButton.dataset.openNode;
    selectedTreeView = "content";
    renderSelectedTreeNode();
    return;
  }

  const button = event.target.closest("button[data-action]");
  if (!button) return;

  const item = {
    type: button.dataset.type,
    course: button.dataset.course || "",
    section: button.dataset.section || "",
    lecture: button.dataset.lecture || "",
  };

  try {
    await startItemDownload(button.dataset.action, item);
  } catch (error) {
    showMessage(error.message);
  }
});

selectFolderBtn.addEventListener("click", async () => {
  clearMessage();
  selectFolderBtn.disabled = true;
  try {
    const result = await requestJson("/api/select-folder", {
      method: "POST",
      body: JSON.stringify({ path: pathInput.value }),
    });
    if (result.path) {
      pathInput.value = result.path;
      await updateDownloadState();
    }
  } catch (error) {
    showMessage(error.message);
  } finally {
    selectFolderBtn.disabled = false;
  }
});

let downloadStateTimer = null;
function scheduleDownloadStateUpdate() {
  clearTimeout(downloadStateTimer);
  downloadStateTimer = setTimeout(updateDownloadState, 250);
}

courseInput.addEventListener("input", scheduleDownloadStateUpdate);
pathInput.addEventListener("input", scheduleDownloadStateUpdate);

settingsToggleBtn.addEventListener("click", () => {
  setSettingsVisible(settingsPanel.hasAttribute("hidden"), true);
});

activityTree.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-node-id]");
  if (!button) return;
  const toggle = event.target.closest("[data-toggle-node]");
  if (toggle && toggle.dataset.toggleNode) {
    userChangedExpansion = true;
    if (expandedTreeIds.has(toggle.dataset.toggleNode)) {
      expandedTreeIds.delete(toggle.dataset.toggleNode);
    } else {
      expandedTreeIds.add(toggle.dataset.toggleNode);
    }
  }
  selectedTreeId = button.dataset.nodeId;
  selectedTreeView = "content";
  renderSelectedTreeNode();
});

expandTreeBtn.addEventListener("click", () => {
  userChangedExpansion = true;
  expandAllTreeNodes();
  renderSelectedTreeNode();
});

collapseTreeBtn.addEventListener("click", () => {
  userChangedExpansion = true;
  collapseAllTreeNodes();
  renderSelectedTreeNode();
});

loginCheckBtn.addEventListener("click", async () => {
  clearMessage();
  loginCheckBtn.disabled = true;
  try {
    if (!loginOpened) {
      loginWindow = window.open(
        "https://www.coursera.org/?authMode=login",
        "courseraLogin",
        "width=1100,height=800"
      );
      if (!loginWindow) {
        throw new Error("Popup was blocked. Allow popups for this app, then click Login again.");
      }
      loginOpened = true;
      setAuthMessage("Checking login...", null);
      await refreshStatus();
      startAuthPolling();
      return;
    }

    const result = await checkAuthOnce(true);
    if (!result.authenticated) {
      setAuthMessage("Check Auth", false);
    }
  } catch (error) {
    setAuthMessage(error.message, false);
    showMessage(error.message);
  } finally {
    loginCheckBtn.disabled = false;
  }
});

init().catch((error) => showMessage(error.message));
