import { useCallback, useEffect, useMemo, useRef, useState, type SetStateAction } from "react";
import { AdminSection } from "./admin";
import { loadAppearance, resolveAppearance, saveAppearance, type Appearance } from "./appearance";
import {
  createMeeting,
  createTranscript,
  deleteMeeting,
  getAuthStatus,
  getHealth,
  getMeeting,
  getSettings,
  getUserSession,
  listMeetings,
  logoutUser,
  retryMeeting,
  stopMeeting,
  restartMeeting,
} from "./api";
import { Composer, UserLogin } from "./composer";
import { connectionClass, connectionDetail, connectionLabel, formatDate, formatDuration, queueFromMeetings, queuePositionLabel } from "./format";
import { MeetingPane, stepLabel } from "./meeting";
import { startPolling } from "./poll";
import type { AdminTab, AuthStatus, Health, Meeting, UserSession } from "./types";

function pathIsAdmin(): boolean {
  return window.location.pathname === "/admin" || window.location.pathname.startsWith("/admin/");
}

function mergeMeetings(prev: Meeting[], rows: Meeting[]): Meeting[] {
  const previous = new Map(prev.map((item) => [item.id, item]));
  return rows.map((row) => {
    const old = previous.get(row.id);
    if (!old || old.status !== row.status) return row;
    if (row.result == null && old.result) {
      return {
        ...row,
        source_type: row.source_type ?? old.source_type,
        calendar_context: row.calendar_context ?? old.calendar_context,
        result: old.result,
        transcript: old.transcript,
        speakers: row.speakers ?? old.speakers,
        segments: row.segments ?? old.segments,
        diarization_note: row.diarization_note ?? old.diarization_note,
        name_review: row.name_review === undefined ? old.name_review : row.name_review,
      };
    }
    return {
      ...row,
      source_type: row.source_type ?? old.source_type,
      calendar_context: row.calendar_context ?? old.calendar_context,
      result: row.result ?? old.result,
      transcript: row.transcript ?? old.transcript,
      speakers: row.speakers ?? old.speakers,
      segments: row.segments ?? old.segments,
      diarization_note: row.diarization_note ?? old.diarization_note,
      name_review: row.name_review === undefined ? old.name_review : row.name_review,
    };
  });
}

export default function App() {
  const [meetings, setMeetingRows] = useState<Meeting[]>([]);
  const dataVersion = useRef(0);
  const meetingVersions = useRef(new Map<string, number>());
  const setMeetings = useCallback((update: SetStateAction<Meeting[]>, meetingId?: string) => {
    dataVersion.current += 1;
    if (meetingId) meetingVersions.current.set(meetingId, dataVersion.current);
    else meetingVersions.current.clear();
    setMeetingRows(update);
  }, []);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [tab, setTab] = useState<"overview" | "actions" | "mom" | "transcript">("overview");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailFailure, setDetailFailure] = useState<{ key: string; message: string } | null>(null);
  const [detailAttempt, setDetailAttempt] = useState(0);
  const [composing, setComposing] = useState(true);
  const [screen, setScreen] = useState<"app" | "admin">(pathIsAdmin() ? "admin" : "app");
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [user, setUser] = useState<UserSession | null>(null);
  const [adminHealth, setAdminHealth] = useState<Health | null>(null);
  const [adminTab, setAdminTab] = useState<AdminTab>("monitoring");
  const [railOpen, setRailOpen] = useState(false);
  const [appearance, setAppearance] = useState(loadAppearance);

  const selected = useMemo(
    () => meetings.find((item) => item.id === selectedId) ?? null,
    [meetings, selectedId],
  );

  const refresh = useCallback(async () => {
    const version = dataVersion.current;
    const rows = await listMeetings();
    if (version === dataVersion.current) setMeetingRows((prev) => mergeMeetings(prev, rows));
    return rows;
  }, []);

  function goApp() {
    setScreen("app");
    if (window.location.pathname !== "/") {
      window.history.pushState({}, "", "/");
    }
  }

  function goAdmin() {
    setScreen("admin");
    if (!pathIsAdmin()) {
      window.history.pushState({}, "", "/admin");
    }
  }

  useEffect(() => {
    const onPop = () => setScreen(pathIsAdmin() ? "admin" : "app");
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    getHealth()
      .then(setHealth)
      .catch(() =>
        setHealth({
          ok: false,
          ffmpeg: false,
          openai: {
            configured: false,
            connected: false,
            hint: null,
            message: "Нет связи с сервером",
            checked_at: null,
          },
          theme: "classic",
        }),
      );
    getAuthStatus().then(setAuth).catch(() => setAuth({ configured: false, authenticated: false }));
    getUserSession()
      .then((session) => {
        setUser(session);
        if (session.authenticated) refresh().catch((err: Error) => setError(err.message));
      })
      .catch(() => setUser({ authenticated: false }));
  }, [refresh]);

  useEffect(() => {
    document.documentElement.dataset.theme = health?.theme === "t2" ? "t2" : "classic";
  }, [health?.theme]);

  useEffect(() => {
    const sync = () => { document.documentElement.dataset.appearance = resolveAppearance(appearance, new Date()); };
    sync();
    if (appearance !== "auto") return;
    const timer = window.setInterval(sync, 30_000);
    document.addEventListener("visibilitychange", sync);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", sync);
    };
  }, [appearance]);

  useEffect(() => {
    if (!composing && !meetings.some((item) => item.id === selectedId) && meetings.length > 0) {
      setSelectedId(meetings[0].id);
    }
  }, [composing, meetings, selectedId]);

  const openai = health?.openai;
  const adminReady = auth?.authenticated === true;
  const userReady = Boolean(user?.authenticated);
  const showComposer = composing || meetings.length === 0;
  const liveQueue = queueFromMeetings(meetings, health?.queue?.limit || 1);
  const archived = meetings.filter(({ status }) => status === "done" || status === "error");
  const archiveLimit = user?.archive_limit || 5;
  const nextExpired = archived.length >= archiveLimit ? archived.at(-1)?.title : undefined;
  const processingKey = meetings
    .filter((item) => (item.status === "done" && item.mail_pending)
      || (item.status !== "done" && item.status !== "error" && item.status !== "review"))
    .map((item) => item.id)
    .join(",");

  useEffect(() => {
    if (!userReady || !processingKey) return;
    return startPolling(async (signal) => {
      const version = dataVersion.current;
      return { version, rows: await listMeetings(signal) };
    }, ({ version, rows }) => {
      if (version === dataVersion.current) setMeetingRows((prev) => mergeMeetings(prev, rows));
    });
  }, [processingKey, userReady]);

  const needsDetails = selected && (
    selected.segments == null || selected.source_type === undefined ||
    (selected.status === "review" && selected.transcript == null) ||
    (selected.status === "done" && selected.result == null)
  );
  const detailKey = userReady && selected && needsDetails &&
    ["review", "done", "error"].includes(selected.status) ? `${selected.id}:${selected.status}` : "";

  useEffect(() => {
    if (!detailKey || !selectedId) return;
    const controller = new AbortController();
    const version = meetingVersions.current.get(selectedId);
    setDetailFailure(null);
    void getMeeting(selectedId, controller.signal)
      .then((full) => {
        if (controller.signal.aborted || version !== meetingVersions.current.get(selectedId)) return;
        setMeetingRows((rows) => rows.map((row) => (row.id === full.id ? full : row)));
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted && version === meetingVersions.current.get(selectedId)) {
          setDetailFailure({ key: detailKey,
            message: err instanceof Error ? err.message : "Не удалось загрузить транскрипт" });
        }
      });
    return () => controller.abort();
  }, [detailKey, selectedId, detailAttempt]);

  useEffect(() => {
    if (screen !== "admin" || !auth?.authenticated) return;
    getSettings()
      .then(setAdminHealth)
      .catch(() => setAuth((current) => ({ configured: current?.configured ?? true, authenticated: false })));
  }, [screen, auth?.authenticated]);

  async function onUpload(file: File | null, title: string, text?: string, calendarEventId?: string) {
    setError(null);
    setBusy(true);
    try {
      const created = text !== undefined
        ? await createTranscript(file, text, title, calendarEventId)
        : await createMeeting(file!, title, calendarEventId);
      setMeetings((rows) => [created, ...rows.filter((row) => row.id !== created.id)], created.id);
      setSelectedId(created.id);
      setTab("overview");
      setComposing(false);
      setRailOpen(false);
      getUserSession().then(setUser).catch(() => undefined);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось загрузить запись");
    } finally {
      setBusy(false);
    }
  }

  async function onRetry(id: string) {
    setError(null);
    try {
      const updated = await retryMeeting(id);
      setMeetings((rows) => rows.map((row) => (row.id === id ? updated : row)), id);
      setSelectedId(id);
      goApp();
      setComposing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось повторить обработку");
    }
  }

  async function onStop(id: string) {
    setError(null);
    try {
      const updated = await stopMeeting(id);
      setMeetings((rows) => rows.map((row) => (row.id === id ? updated : row)), id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось остановить обработку");
    }
  }

  async function onRestart(id: string) {
    if (!window.confirm("Остановить текущую обработку и начать распознавание заново?")) return;
    setError(null);
    try {
      const updated = await restartMeeting(id);
      setMeetings((rows) => rows.map((row) => (row.id === id ? updated : row)), id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось запустить обработку заново");
    }
  }

  async function onDelete(id: string) {
    if (!window.confirm("Удалить встречу и протокол?")) return;
    try {
      await deleteMeeting(id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось удалить встречу");
      return;
    }
    const rows = await refresh();
    setSelectedId((current) => {
      if (current !== id) return current;
      return rows[0]?.id ?? null;
    });
  }

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        К содержимому
      </a>
      <header className="topbar">
        <div className="brand">
          <span className="mark">MoM</span>
          <span className="brand-sub">Minutes of Meeting</span>
        </div>
        {userReady && (
          <button
            type="button"
            className="ghost rail-toggle"
            onClick={() => setRailOpen((open) => !open)}
            aria-expanded={railOpen}
          >
            {railOpen ? "Скрыть архив" : "Архив"}
          </button>
        )}
        <nav className="app-tabs" aria-label="Разделы">
          <button className={screen === "app" ? "active" : ""} onClick={goApp}>
            Встречи
          </button>
        </nav>
        <p className={`top-status ${userReady ? "ok" : "missing"}`}>
          {userReady ? `${user?.email}` : "Нет входа в профиль"}
        </p>
        <label className="appearance-picker">
          <span>Тема</span>
          <select title="В режиме «По времени» светлая тема с 07:00 до 19:00 по часам устройства" value={appearance} onChange={(event) => {
            const value = event.target.value as Appearance;
            saveAppearance(value);
            setAppearance(value);
          }}>
            <option value="auto">По времени</option>
            <option value="light">Светлая</option>
            <option value="dark">Тёмная</option>
          </select>
        </label>
        {adminReady && (
          <button type="button" className={`top-key ${health ? connectionClass(openai) : ""}`} onClick={goAdmin}>
            {health ? connectionLabel(openai) : "Проверяю ключ"}
          </button>
        )}
        {screen === "admin" ? (
          <button className="ghost" type="button" onClick={goApp}>
            К встречам
          </button>
        ) : adminReady && (
          <button className="ghost admin-link" type="button" onClick={goAdmin}>
            Админка
          </button>
        )}
      </header>
      <div className="workspace">
        <aside className={`rail${railOpen ? " open" : ""}`}>
          {userReady ? (
            <button
              className="primary"
              onClick={() => {
                goApp();
                setComposing(true);
                setRailOpen(false);
              }}
            >
              Новая запись
            </button>
          ) : (
            <p className="rail-label">Войдите, чтобы видеть архив</p>
          )}
          <div className="rail-label">
            Архив{userReady && user?.archive_limit ? ` · ${archived.length}/${archiveLimit}` : ""}
          </div>
          <ul className="meeting-list">
            {meetings.map((item) => (
              <li key={item.id}>
                <button
                  className={item.id === selectedId && !showComposer && screen === "app" ? "active" : ""}
                  onClick={() => {
                    setSelectedId(item.id);
                    setComposing(false);
                    goApp();
                    setTab("overview");
                    setRailOpen(false);
                  }}
                >
                  <span className={`dot ${item.status}`} />
                  <span className="meeting-copy">
                    <strong>{item.title}</strong>
                    <small>
                      {item.status === "review"
                        ? "Проверьте спикеров"
                        : item.status !== "done" && item.status !== "error"
                        ? item.status === "queued"
                          ? `В очереди · ${queuePositionLabel(item.queue_ahead)}`
                          : `${item.status_message || stepLabel(item.status)}${typeof item.progress === "number" ? ` · ${item.progress}%` : ""}`
                        : `${formatDate(item.created_at)}${item.duration_seconds ? ` · ${formatDuration(item.duration_seconds)}` : ""}`}
                    </small>
                  </span>
                </button>
              </li>
            ))}
          </ul>
          {userReady && (
            <button
              type="button"
              className="ghost"
              style={{ margin: "8px 16px" }}
              onClick={() => {
                void logoutUser().then((session) => {
                  setUser(session);
                  setMeetings([]);
                  setSelectedId(null);
                  setComposing(false);
                });
              }}
            >
              Выйти из профиля
            </button>
          )}
          {adminReady && (
            <button type="button" className={`conn ${health ? connectionClass(openai) : ""}`} onClick={goAdmin}>
              <span className="conn-dot" />
              <span>
                <strong>{health ? connectionLabel(openai) : "Проверяю ключ"}</strong>
                {health && <small>{connectionDetail(openai)}</small>}
              </span>
            </button>
          )}
          {health && !health.ffmpeg && <p className="health-warn">Не найден ffmpeg.</p>}
          {health?.disk && !health.disk.ok && (
            <p className="health-warn">Мало места на диске. Новые записи не принимаются, пока не освободится место.</p>
          )}
        </aside>

        <main className="stage" id="main">
          {error && <div className="banner" role="alert">{error}</div>}
          {detailFailure?.key === detailKey && <div className="banner" role="alert">
            {detailFailure.message}{" "}
            <button type="button" className="ghost" onClick={() => setDetailAttempt((attempt) => attempt + 1)}>
              Повторить загрузку
            </button>
          </div>}
          {screen === "admin" ? (
            <AdminSection
              auth={auth}
              health={adminHealth}
              tab={adminTab}
              onTab={setAdminTab}
              onAuth={(next) => {
                setAuth(next);
                if (!next.authenticated) {
                  getHealth().then(setHealth).catch(() => undefined);
                }
              }}
              onHealth={(next) => {
                setAdminHealth(next);
                setHealth((current) =>
                  current
                    ? {
                        ...current,
                        ok: next.ok,
                        ffmpeg: next.ffmpeg,
                        theme: next.theme,
                        queue: next.queue,
                        smtp: { configured: Boolean(next.smtp?.configured) },
                        ldap: {
                          enabled: Boolean(next.ldap?.enabled),
                          configured: Boolean(next.ldap?.configured),
                        },
                        disk: next.disk,
                        openai: {
                          ...current.openai,
                          configured: next.openai.configured,
                          connected: next.openai.connected,
                          auth_rejected: next.openai.auth_rejected,
                          rate_limited: next.openai.rate_limited,
                          model_unavailable: next.openai.model_unavailable,
                          message: next.openai.message,
                          checked_at: next.openai.checked_at,
                          provider: next.openai.provider,
                          provider_label: next.openai.provider_label,
                          hint: null,
                        },
                      }
                    : current,
                );
              }}
            />
          ) : !userReady ? (
            <UserLogin
              onLogin={async (session) => {
                setUser(session);
                setComposing(true);
                setError(null);
                const rows = await refresh();
                setSelectedId(rows[0]?.id ?? null);
              }}
            />
          ) : showComposer ? (
            <Composer
              busy={busy}
              keyStatus={health?.openai ?? null}
              queue={liveQueue}
              mailReady={Boolean(health?.smtp?.configured)}
              maxUploadBytes={health?.max_upload_bytes}
              nextExpired={nextExpired}
              userEmail={user?.email || undefined}
              onOpenCalendarSettings={adminReady ? () => { setAdminTab("exchange"); goAdmin(); } : undefined}
              onOpenSettings={adminReady ? goAdmin : undefined}
              onUpload={onUpload}
            />
          ) : selected ? (
            <MeetingPane
              key={selected.id}
              meeting={selected}
              tab={tab}
              mailEnabled={Boolean(health?.smtp?.configured)}
              directoryEnabled={Boolean(health?.ldap?.configured)}
              onTab={setTab}
              onRetry={() => void onRetry(selected.id)}
              onStop={() => void onStop(selected.id)}
              onRestart={() => void onRestart(selected.id)}
              onDelete={() => onDelete(selected.id)}
              onUpdated={(next) => {
                setMeetings((rows) => rows.map((row) => (row.id === next.id ? next : row)), next.id);
              }}
            />
          ) : (
            <Composer
              busy={busy}
              keyStatus={health?.openai ?? null}
              queue={liveQueue}
              mailReady={Boolean(health?.smtp?.configured)}
              maxUploadBytes={health?.max_upload_bytes}
              nextExpired={nextExpired}
              userEmail={user?.email || undefined}
              onOpenCalendarSettings={adminReady ? () => { setAdminTab("exchange"); goAdmin(); } : undefined}
              onOpenSettings={adminReady ? goAdmin : undefined}
              onUpload={onUpload}
            />
          )}
        </main>
      </div>
    </div>
  );
}
