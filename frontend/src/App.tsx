import { useCallback, useEffect, useMemo, useRef, useState, type SetStateAction } from "react";
import { AdminSection } from "./admin";
import { loadAppearance, resolveAppearance, saveAppearance, type Appearance } from "./appearance";
import {
  createMeeting,
  createTranscript,
  deleteMeeting,
  getAuthStatus,
  getMeeting,
  getSettings,
  getUserSession,
  listMeetings,
  logoutUser,
  retryMeeting,
  stopMeeting,
  restartMeeting,
} from "./api";
import { Composer, UserLogin, emptyComposerDraft } from "./composer";
import { ApiError, onSessionExpired, sessionChanged } from "./http";
import { connectionClass, connectionDetail, connectionLabel, formatDate, formatDuration, queueFromMeetings, queuePositionLabel } from "./format";
import { MeetingPane, stepLabel, type ProtocolDraft } from "./meeting";
import { startPolling } from "./poll";
import { useServerHealth } from "./server-health";
import type { AdminTab, AuthStatus, Health, Meeting, UserSession } from "./types";

function pathIsAdmin(): boolean {
  return window.location.pathname === "/admin" || window.location.pathname.startsWith("/admin/");
}

type ArchiveMeeting = Meeting & { detailsStale?: boolean };

function mergeMeetings(prev: ArchiveMeeting[], rows: Meeting[]): ArchiveMeeting[] {
  const previous = new Map(prev.map((item) => [item.id, item]));
  return rows.map((row) => {
    const old = previous.get(row.id);
    if (!old) return row;
    if (old.body_version !== undefined && row.body_version !== undefined && old.body_version > row.body_version) return old;
    if (old.status !== row.status || (row.source_type !== undefined && row.segments != null)) return row;
    return {
      ...row,
      detailsStale: old.detailsStale || old.body_version !== row.body_version,
      source_type: row.source_type ?? old.source_type,
      calendar_context: row.calendar_context ?? old.calendar_context,
      result: row.result ?? old.result,
      result_revision: row.result == null ? old.result_revision : row.result_revision,
      transcript: row.transcript ?? old.transcript,
      speakers: row.speakers ?? old.speakers,
      segments: row.segments ?? old.segments,
      diarization_note: row.diarization_note ?? old.diarization_note,
      name_review: row.name_review === undefined ? old.name_review : row.name_review,
    };
  });
}

export default function App() {
  const [meetings, setMeetingRows] = useState<ArchiveMeeting[]>([]);
  const dataVersion = useRef(0);
  const protocolDirty = useRef(false);
  const protocolDraft = useRef<{ ownerId: string; meetingId: string; editor: ProtocolDraft } | null>(null);
  const onEditorDirty = useCallback((dirty: boolean) => { protocolDirty.current = dirty; }, []);
  const canLeaveEditor = useCallback(() => {
    if (protocolDirty.current && !window.confirm("Покинуть встречу и отбросить несохранённые правки протокола?")) return false;
    protocolDraft.current = null;
    return true;
  }, []);
  const meetingVersions = useRef(new Map<string, number>());
  const setMeetings = useCallback((update: SetStateAction<Meeting[]>, meetingId?: string) => {
    dataVersion.current += 1;
    if (meetingId) meetingVersions.current.set(meetingId, dataVersion.current);
    else meetingVersions.current.clear();
    setMeetingRows(update);
  }, []);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const { health, setHealth, unavailable: serverUnavailable, recheck: recheckHealth } = useServerHealth();
  const [tab, setTab] = useState<"overview" | "actions" | "mom" | "transcript">("overview");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [archiveState, setArchiveState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [archiveError, setArchiveError] = useState<string | null>(null);
  const archiveRequest = useRef<{ promise: Promise<Meeting[]>; controller: AbortController } | null>(null);
  const [expired, setExpired] = useState({ user: false, admin: false });
  const [draft, setDraft] = useState(emptyComposerDraft);
  const draftOwner = useRef<string | undefined>();
  const userGeneration = useRef(0);
  const [detailFailure, setDetailFailure] = useState<{ key: string; message: string } | null>(null);
  const [detailAttempt, setDetailAttempt] = useState(0);
  const [composing, setComposing] = useState(true);
  const [screen, setScreen] = useState<"app" | "admin">(pathIsAdmin() ? "admin" : "app");
  const [auth, setAuth] = useState<AuthStatus | null>(null);
  const [user, setUser] = useState<UserSession | null>(null);
  const [adminHealth, setAdminHealth] = useState<Health | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [settingsAttempt, setSettingsAttempt] = useState(0);
  const [adminTab, setAdminTab] = useState<AdminTab>("monitoring");
  const [railOpen, setRailOpen] = useState(false);
  const [appearance, setAppearance] = useState(loadAppearance);

  const selected = useMemo(
    () => meetings.find((item) => item.id === selectedId) ?? null,
    [meetings, selectedId],
  );

  const invalidateUserRequests = useCallback(() => {
    userGeneration.current += 1;
    dataVersion.current += 1;
    archiveRequest.current?.controller.abort();
    archiveRequest.current = null;
    setArchiveState("idle");
    setArchiveError(null);
    setBusy(false);
    setDetailFailure(null);
  }, []);

  const refresh = useCallback(() => {
    if (archiveRequest.current) return archiveRequest.current.promise;
    const version = dataVersion.current;
    const controller = new AbortController();
    setArchiveState("loading");
    const promise = listMeetings(controller.signal).then((rows) => {
      if (!controller.signal.aborted) {
        if (version === dataVersion.current) setMeetingRows((prev) => mergeMeetings(prev, rows));
        setArchiveState("ready");
        setArchiveError(null);
      }
      return rows;
    }).catch((err: unknown) => {
      if (!controller.signal.aborted && !(err instanceof ApiError && err.session)) {
        setArchiveState("error");
        setArchiveError(err instanceof Error ? err.message : "Не удалось загрузить архив");
      }
      throw err;
    }).finally(() => {
      if (archiveRequest.current?.controller === controller) archiveRequest.current = null;
    });
    archiveRequest.current = { promise, controller };
    return promise;
  }, []);

  useEffect(() => onSessionExpired((scope) => {
    setExpired((old) => ({ ...old, [scope]: true }));
    if (scope === "user") {
      invalidateUserRequests();
      setUser((old) => ({ ...old, authenticated: false }));
      // Calendar invitation tokens belong to the expired session; keep the text/file draft.
      setDraft((old) => ({ ...old, calendar: null }));
    } else {
      setAuth((old) => ({ configured: old?.configured ?? true, authenticated: false }));
      setAdminHealth(null);
      setSettingsError(null);
    }
  }), [invalidateUserRequests]);

  function goApp() {
    setScreen("app");
    if (window.location.pathname !== "/") {
      window.history.pushState({}, "", "/");
    }
  }

  function goAdmin() {
    if (!canLeaveEditor()) return;
    setScreen("admin");
    if (!pathIsAdmin()) {
      window.history.pushState({}, "", "/admin");
    }
  }

  useEffect(() => {
    const onPop = () => {
      if (pathIsAdmin() && !canLeaveEditor()) { window.history.pushState({}, "", "/"); return; }
      setScreen(pathIsAdmin() ? "admin" : "app");
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [canLeaveEditor]);

  useEffect(() => {
    let disposed = false;
    const generation = userGeneration.current;
    getAuthStatus().then((next) => { sessionChanged("admin"); setAuth(next); })
      .catch(() => setAuth({ configured: true, authenticated: false }));
    getUserSession()
      .then((session) => {
        if (disposed || generation !== userGeneration.current) return;
        invalidateUserRequests();
        setUser(session);
        sessionChanged("user");
        draftOwner.current = session.id ?? undefined;
      })
      .catch(() => {
        if (!disposed && generation === userGeneration.current) setUser({ authenticated: false });
      });
    return () => { disposed = true; };
  }, [invalidateUserRequests]);

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
  const renderedUserGeneration = userGeneration.current;
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
    if (!userReady) return;
    const reload = () => { void refresh().catch(() => undefined); };
    window.addEventListener("online", reload);
    window.addEventListener("focus", reload);
    reload();
    return () => {
      window.removeEventListener("online", reload);
      window.removeEventListener("focus", reload);
      archiveRequest.current?.controller.abort();
      archiveRequest.current = null;
    };
  }, [userReady, refresh]);

  useEffect(() => {
    if (!userReady || archiveState !== "error") return;
    const timer = window.setTimeout(() => { void refresh().catch(() => undefined); }, 5000);
    return () => window.clearTimeout(timer);
  }, [userReady, archiveState, refresh]);

  useEffect(() => {
    if (!userReady || !processingKey) return;
    return startPolling(async (signal) => {
      const version = dataVersion.current;
      return { version, rows: await listMeetings(signal) };
    }, ({ version, rows }) => {
      if (version === dataVersion.current) {
        setMeetingRows((prev) => mergeMeetings(prev, rows));
        setArchiveState("ready");
        setArchiveError(null);
      }
    });
  }, [processingKey, userReady]);

  const needsDetails = selected && (
    selected.detailsStale || selected.segments == null || selected.source_type === undefined ||
    (selected.status === "review" && selected.transcript == null) ||
    (selected.status === "done" && selected.result == null)
  );
  const detailKey = userReady && selected && needsDetails &&
    ["review", "done", "error"].includes(selected.status) ? `${selected.id}:${selected.status}:${selected.body_version ?? ""}` : "";

  useEffect(() => {
    if (!detailKey || !selectedId) return;
    const controller = new AbortController();
    const version = meetingVersions.current.get(selectedId);
    const generation = userGeneration.current;
    setDetailFailure(null);
    void getMeeting(selectedId, controller.signal)
      .then((full) => {
        if (controller.signal.aborted || generation !== userGeneration.current || version !== meetingVersions.current.get(selectedId)) return;
        setMeetingRows((rows) => rows.map(row => row.id === full.id &&
          (row.body_version ?? 0) <= (full.body_version ?? 0) ? full : row));
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted && generation === userGeneration.current && version === meetingVersions.current.get(selectedId)) {
          setDetailFailure({ key: detailKey,
            message: err instanceof Error ? err.message : "Не удалось загрузить транскрипт" });
        }
      });
    return () => controller.abort();
  }, [detailKey, selectedId, detailAttempt]);

  useEffect(() => {
    if (screen !== "admin" || !auth?.authenticated) return;
    let disposed = false;
    setSettingsError(null);
    getSettings()
      .then((next) => { if (!disposed) setAdminHealth(next); })
      .catch((err: unknown) => {
        if (!disposed && !(err instanceof ApiError && err.session)) {
          setSettingsError(err instanceof Error ? err.message : "Не удалось загрузить настройки");
        }
      });
    return () => { disposed = true; };
  }, [screen, auth?.authenticated, settingsAttempt]);

  async function onUpload(file: File | null, title: string, text?: string, calendarEventId?: string) {
    const generation = userGeneration.current;
    setError(null);
    setBusy(true);
    try {
      const created = text !== undefined
        ? await createTranscript(file, text, title, calendarEventId)
        : await createMeeting(file!, title, calendarEventId);
      if (generation !== userGeneration.current) return;
      setMeetings((rows) => [created, ...rows.filter((row) => row.id !== created.id)], created.id);
      setSelectedId(created.id);
      setTab("overview");
      setComposing(false);
      setDraft(emptyComposerDraft());
      setRailOpen(false);
      getUserSession().then((session) => {
        if (generation !== userGeneration.current) return;
        if (session.authenticated && session.id === draftOwner.current) setUser(session);
        else if (!session.authenticated) {
          invalidateUserRequests();
          sessionChanged("user");
          setUser(session);
          setExpired((old) => ({ ...old, user: true }));
        }
      }).catch(() => undefined);
    } catch (err) {
      if (generation === userGeneration.current) setError(err instanceof Error ? err.message : "Не удалось загрузить запись");
    } finally {
      if (generation === userGeneration.current) setBusy(false);
    }
  }

  async function onRetry(id: string, revision?: string) {
    const generation = userGeneration.current;
    setError(null);
    try {
      const updated = await retryMeeting(id, revision);
      if (generation !== userGeneration.current) return;
      setMeetings((rows) => rows.map((row) => (row.id === id ? updated : row)), id);
      setSelectedId(id);
      goApp();
      setComposing(false);
    } catch (err) {
      if (generation === userGeneration.current) setError(err instanceof Error ? err.message : "Не удалось повторить обработку");
    }
  }

  async function onStop(id: string) {
    const generation = userGeneration.current;
    setError(null);
    try {
      const updated = await stopMeeting(id);
      if (generation !== userGeneration.current) return;
      setMeetings((rows) => rows.map((row) => (row.id === id ? updated : row)), id);
    } catch (err) {
      if (generation === userGeneration.current) setError(err instanceof Error ? err.message : "Не удалось остановить обработку");
    }
  }

  async function onRestart(id: string) {
    const generation = userGeneration.current;
    if (!window.confirm("Остановить текущую обработку и начать распознавание заново?")) return;
    setError(null);
    try {
      const updated = await restartMeeting(id);
      if (generation !== userGeneration.current) return;
      setMeetings((rows) => rows.map((row) => (row.id === id ? updated : row)), id);
    } catch (err) {
      if (generation === userGeneration.current) setError(err instanceof Error ? err.message : "Не удалось запустить обработку заново");
    }
  }

  async function onDelete(id: string) {
    const generation = userGeneration.current;
    if (!window.confirm("Удалить встречу и протокол?")) return;
    try {
      await deleteMeeting(id);
    } catch (err) {
      if (generation === userGeneration.current) setError(err instanceof Error ? err.message : "Не удалось удалить встречу");
      return;
    }
    if (generation !== userGeneration.current) return;
    // The deletion succeeded even if reloading the remaining archive fails.
    setMeetings((rows) => rows.filter((row) => row.id !== id), id);
    const rows = await refresh().catch(() => []);
    if (generation !== userGeneration.current) return;
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
                if (!canLeaveEditor()) return;
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
          {userReady && archiveState === "loading" && <p role="status">Загружаю архив…</p>}
          {userReady && archiveError && <div className="banner" role="alert">
            Не удалось обновить архив. {archiveError}{" "}Повторим автоматически.{" "}
            <button type="button" className="ghost" disabled={archiveState === "loading"}
              onClick={() => { void refresh().catch(() => undefined); }}>Повторить загрузку архива</button>
          </div>}
          <ul className="meeting-list">
            {(userReady ? meetings : []).map((item) => (
              <li key={item.id}>
                <button
                  className={item.id === selectedId && !showComposer && screen === "app" ? "active" : ""}
                  onClick={() => {
                    if (item.id !== selectedId && !canLeaveEditor()) return;
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
                const generation = userGeneration.current;
                if (!canLeaveEditor()) return;
                void logoutUser().then((session) => {
                  if (generation !== userGeneration.current) return;
                  invalidateUserRequests();
                  sessionChanged("user");
                  setUser(session);
                  setMeetings([]);
                  setSelectedId(null);
                  setComposing(false);
                  setDraft(emptyComposerDraft());
                  draftOwner.current = undefined;
                  setExpired((old) => ({ ...old, user: false }));
                }).catch((err: Error) => {
                  if (generation === userGeneration.current) setError(err.message);
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
          {serverUnavailable && <div className="banner" role="status">
            Нет связи с сервером. Пробуем подключиться автоматически.{" "}
            <button type="button" className="ghost" onClick={recheckHealth}>Проверить подключение</button>
          </div>}
          {error && <div className="banner" role="alert">{error}</div>}
          {screen === "app" && expired.user && <div className="banner" role="alert">
            Сессия истекла. Войдите в тот же профиль, чтобы продолжить работу с черновиком в этой вкладке.
          </div>}
          {screen === "admin" && expired.admin && <div className="banner" role="alert">
            Сессия администратора истекла. Войдите снова.
          </div>}
          {screen === "admin" && adminReady && settingsError && <div className="banner" role="alert">
            Не удалось загрузить настройки. {settingsError}{" "}
            <button type="button" className="ghost" onClick={() => setSettingsAttempt((attempt) => attempt + 1)}>
              Повторить загрузку настроек
            </button>
          </div>}
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
                sessionChanged("admin");
                setAuth(next);
                setExpired((old) => ({ ...old, admin: false }));
                if (!next.authenticated) {
                  recheckHealth();
                }
              }}
              onHealth={(next) => {
                setAdminHealth(next);
                setHealth((current) => ({
                  ...current,
                  ok: next.ok,
                  ffmpeg: next.ffmpeg,
                  max_upload_bytes: next.max_upload_bytes ?? current?.max_upload_bytes,
                  theme: next.theme,
                  queue: next.queue,
                  smtp: { configured: Boolean(next.smtp?.configured) },
                  ldap: {
                    enabled: Boolean(next.ldap?.enabled),
                    configured: Boolean(next.ldap?.configured),
                  },
                  disk: next.disk,
                  openai: {
                    ...current?.openai,
                    configured: next.openai.configured,
                    connected: next.openai.connected,
                    asr_ready: next.openai.asr_ready,
                    auth_rejected: next.openai.auth_rejected,
                    rate_limited: next.openai.rate_limited,
                    model_unavailable: next.openai.model_unavailable,
                    message: next.openai.message,
                    checked_at: next.openai.checked_at,
                    provider: next.openai.provider,
                    provider_label: next.openai.provider_label,
                    hint: null,
                  },
                }));
              }}
            />
          ) : !userReady ? (
            <UserLogin
              onLogin={async (session) => {
                invalidateUserRequests();
                sessionChanged("user");
                const sameUser = Boolean(session.id) && draftOwner.current === session.id;
                if (!sameUser) {
                  protocolDraft.current = null;
                  setMeetings([]);
                  setSelectedId(null);
                  setDraft(emptyComposerDraft());
                  setComposing(true);
                }
                draftOwner.current = session.id ?? undefined;
                setUser(session);
                setExpired((old) => ({ ...old, user: false }));
                setError(null);
              }}
            />
          ) : showComposer ? (
            <Composer
              draft={draft}
              onDraftChange={setDraft}
              busy={busy}
              serverUnavailable={serverUnavailable}
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
              ownerEmail={user?.email ?? undefined}
              tab={tab}
              mailEnabled={Boolean(health?.smtp?.configured)}
              directoryEnabled={Boolean(health?.ldap?.configured)}
              onTab={setTab}
              onEditorDirty={onEditorDirty}
              restoredDraft={protocolDraft.current && protocolDraft.current.ownerId === user?.id &&
                protocolDraft.current.meetingId === selected.id ? protocolDraft.current.editor : null}
              onDraftChange={editor => {
                if (renderedUserGeneration !== userGeneration.current || !user?.id) return;
                protocolDraft.current = editor ? { ownerId: user.id, meetingId: selected.id, editor } : null;
              }}
              onRetry={revision => void onRetry(selected.id, revision)}
              onStop={() => void onStop(selected.id)}
              onRestart={() => void onRestart(selected.id)}
              onDelete={() => onDelete(selected.id)}
              onUpdated={(next) => {
                if (renderedUserGeneration !== userGeneration.current) return;
                setMeetings((rows) => rows.map(row => row.id === next.id &&
                  (row.body_version ?? 0) <= (next.body_version ?? 0) ? next : row), next.id);
              }}
            />
          ) : (
            <Composer
              draft={draft}
              onDraftChange={setDraft}
              busy={busy}
              serverUnavailable={serverUnavailable}
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
