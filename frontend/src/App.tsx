import { useCallback, useEffect, useMemo, useState } from "react";
import { AdminSection } from "./admin";
import {
  createMeeting,
  deleteMeeting,
  getAuthStatus,
  getHealth,
  getMeeting,
  getSettings,
  getUserSession,
  listMeetings,
  logoutUser,
  retryMeeting,
} from "./api";
import { Composer, UserLogin } from "./composer";
import { connectionClass, connectionDetail, connectionLabel, formatDate, formatDuration, queueFromMeetings } from "./format";
import { MeetingPane, stepLabel } from "./meeting";
import type { AdminTab, AuthStatus, Health, Meeting, UserSession } from "./types";

function pathIsAdmin(): boolean {
  return window.location.pathname === "/admin" || window.location.pathname.startsWith("/admin/");
}

function mergeMeetings(prev: Meeting[], rows: Meeting[]): Meeting[] {
  const previous = new Map(prev.map((item) => [item.id, item]));
  return rows.map((row) => {
    const old = previous.get(row.id);
    if (!old) return row;
    if (row.result == null && old.result) {
      return {
        ...row,
        result: old.result,
        transcript: old.transcript,
        speakers: row.speakers ?? old.speakers,
        segments: row.segments ?? old.segments,
        diarization_note: row.diarization_note ?? old.diarization_note,
      };
    }
    return {
      ...row,
      result: row.result ?? old.result,
      transcript: row.transcript ?? old.transcript,
      speakers: row.speakers ?? old.speakers,
      segments: row.segments ?? old.segments,
      diarization_note: row.diarization_note ?? old.diarization_note,
    };
  });
}

export default function App() {
  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [tab, setTab] = useState<"overview" | "actions" | "mom" | "transcript">("overview");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [composing, setComposing] = useState(false);
  const [screen, setScreen] = useState<"app" | "admin">(pathIsAdmin() ? "admin" : "app");
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [user, setUser] = useState<UserSession | null>(null);
  const [adminHealth, setAdminHealth] = useState<Health | null>(null);
  const [adminTab, setAdminTab] = useState<AdminTab>("llm");
  const [railOpen, setRailOpen] = useState(false);

  const selected = useMemo(
    () => meetings.find((item) => item.id === selectedId) ?? null,
    [meetings, selectedId],
  );

  const refresh = useCallback(async () => {
    const rows = await listMeetings();
    setMeetings((prev) => mergeMeetings(prev, rows));
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
    if (!composing && !selectedId && meetings.length > 0) {
      setSelectedId(meetings[0].id);
    }
  }, [composing, meetings, selectedId]);

  const openai = health?.openai;
  const keyReady = Boolean(openai?.connected);
  const userReady = Boolean(user?.authenticated);
  const showComposer = composing || meetings.length === 0;
  const liveQueue = queueFromMeetings(meetings, health?.queue?.limit || 1);
  const processingKey = meetings
    .filter((item) => item.status !== "done" && item.status !== "error")
    .map((item) => item.id)
    .join(",");

  useEffect(() => {
    if (!userReady || !processingKey) return;
    const timer = window.setInterval(async () => {
      try {
        const rows = await listMeetings();
        setMeetings((prev) => mergeMeetings(prev, rows));
      } catch {
        /* keep last known state */
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [processingKey, userReady]);

  useEffect(() => {
    if (!userReady || !selectedId) return;
    const current = meetings.find((item) => item.id === selectedId);
    if (!current) return;
    if (current.status !== "done" && current.status !== "error") return;
    if (current.result) return;
    void getMeeting(selectedId)
      .then((full) => {
        setMeetings((rows) => rows.map((row) => (row.id === full.id ? full : row)));
      })
      .catch(() => undefined);
  }, [meetings, selectedId, userReady]);

  useEffect(() => {
    if (screen !== "admin" || !auth?.authenticated) return;
    getSettings()
      .then(setAdminHealth)
      .catch(() => setAuth((current) => ({ configured: current?.configured ?? true, authenticated: false })));
  }, [screen, auth?.authenticated]);

  async function onUpload(file: File, title: string) {
    setError(null);
    setBusy(true);
    try {
      const created = await createMeeting(file, title);
      setMeetings((rows) => [created, ...rows.filter((row) => row.id !== created.id)].slice(0, user?.archive_limit || 5));
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
      setMeetings((rows) => rows.map((row) => (row.id === id ? updated : row)));
      setSelectedId(id);
      goApp();
      setComposing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось повторить обработку");
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
        <button type="button" className={`top-key ${connectionClass(openai)}`} onClick={goAdmin}>
          {connectionLabel(openai)}
        </button>
        {screen === "admin" ? (
          <button className="ghost" type="button" onClick={goApp}>
            К встречам
          </button>
        ) : (
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
            Архив{userReady && user?.archive_limit ? ` · ${meetings.length}/${user.archive_limit}` : ""}
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
                      {item.status !== "done" && item.status !== "error"
                        ? item.status === "queued"
                          ? item.status_message || stepLabel(item.status)
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
          <button type="button" className={`conn ${connectionClass(openai)}`} onClick={goAdmin}>
            <span className="conn-dot" />
            <span>
              <strong>{connectionLabel(openai)}</strong>
              <small>{connectionDetail(openai)}</small>
            </span>
          </button>
          {health && !health.ffmpeg && <p className="health-warn">Не найден ffmpeg.</p>}
        </aside>

        <main className="stage" id="main">
          {error && <div className="banner" role="alert">{error}</div>}
          {screen === "admin" ? (
            <AdminSection
              auth={auth}
              health={adminHealth}
              tab={adminTab}
              onTab={setAdminTab}
              onAuth={setAuth}
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
                        openai: {
                          ...current.openai,
                          configured: next.openai.configured,
                          connected: next.openai.connected,
                          auth_rejected: next.openai.auth_rejected,
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
                setError(null);
                const rows = await refresh();
                setSelectedId(rows[0]?.id ?? null);
              }}
            />
          ) : showComposer ? (
            <Composer
              busy={busy}
              keyReady={keyReady}
              queue={liveQueue}
              mailReady={Boolean(health?.smtp?.configured)}
              onOpenSettings={goAdmin}
              onUpload={onUpload}
            />
          ) : selected ? (
            <MeetingPane
              meeting={selected}
              tab={tab}
              mailEnabled={Boolean(health?.smtp?.configured)}
              onTab={setTab}
              onRetry={() => void onRetry(selected.id)}
              onDelete={() => onDelete(selected.id)}
              onUpdated={(next) => {
                setMeetings((rows) => rows.map((row) => (row.id === next.id ? next : row)));
              }}
            />
          ) : (
            <Composer
              busy={busy}
              keyReady={keyReady}
              queue={liveQueue}
              mailReady={Boolean(health?.smtp?.configured)}
              onOpenSettings={goAdmin}
              onUpload={onUpload}
            />
          )}
        </main>
      </div>
    </div>
  );
}
