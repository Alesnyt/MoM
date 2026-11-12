import { useEffect, useState } from "react";
import { saveLdap, testLdap } from "./api";
import { DirectoryCachePanel } from "./directory-cache";
import type { Health } from "./types";

export function LdapPanel({ health, onChange }: { health: Health | null; onChange: (health: Health) => void }) {
  const ldap = health?.ldap;
  const [enabled, setEnabled] = useState(Boolean(ldap?.enabled));
  const [url, setUrl] = useState(ldap?.url || "");
  const [bindDn, setBindDn] = useState(ldap?.bind_dn || "");
  const [bindPassword, setBindPassword] = useState("");
  const [baseDn, setBaseDn] = useState(ldap?.base_dn || "");
  const [userFilter, setUserFilter] = useState(ldap?.user_filter || "(mail={username})");
  const [starttls, setStarttls] = useState(Boolean(ldap?.starttls));
  const [tlsVerify, setTlsVerify] = useState(ldap?.tls_verify !== false);
  const [emailAttr, setEmailAttr] = useState(ldap?.email_attr || "mail");
  const [probe, setProbe] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!health?.ldap) return;
    setEnabled(Boolean(health.ldap.enabled));
    setUrl(health.ldap.url || "");
    setBindDn(health.ldap.bind_dn || "");
    setBaseDn(health.ldap.base_dn || "");
    setUserFilter(health.ldap.user_filter || "(mail={username})");
    setStarttls(Boolean(health.ldap.starttls));
    setTlsVerify(health.ldap.tls_verify !== false);
    setEmailAttr(health.ldap.email_attr || "mail");
  }, [health]);

  return (
    <>
    <form
      className="key-box"
      onSubmit={(event) => {
        event.preventDefault();
        setError(null);
        setNote(null);
        setBusy("save");
        void saveLdap({
          enabled,
          url: url.trim(),
          bind_dn: bindDn.trim(),
          bind_password: bindPassword,
          base_dn: baseDn.trim(),
          user_filter: userFilter.trim() || "(mail={username})",
          starttls,
          tls_verify: tlsVerify,
          email_attr: emailAttr.trim() || "mail",
        })
          .then((next) => {
            setBindPassword("");
            onChange(next);
            setNote("Настройки каталога сохранены");
          })
          .catch((err: Error) => setError(err.message))
          .finally(() => setBusy(null));
      }}
    >
      <p>
        {ldap?.configured
          ? "Каталог включён. Пользователю можно выбрать вход через LDAP."
          : "Пока каталог выключен, новые пользователи входят только локальным паролем."}
      </p>
      <div className="field-stack">
        <label className="field-check">
          <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
          Включить вход через каталог
        </label>
        <label className="field-label">
          Адрес
          <input
            className="title-input"
            placeholder="ldaps://ldap.example.com:636"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            autoComplete="off"
          />
        </label>
        <label className="field-label">
          Служебная учётная запись (bind DN)
          <input
            className="title-input"
            placeholder="cn=mom,ou=services,dc=example,dc=com"
            value={bindDn}
            onChange={(event) => setBindDn(event.target.value)}
            autoComplete="off"
          />
        </label>
        <label className="field-label">
          Пароль bind
          <input
            className="title-input"
            type="password"
            autoComplete="new-password"
            placeholder={ldap?.has_password ? "Задан, оставьте пустым чтобы не менять" : "Пароль"}
            value={bindPassword}
            onChange={(event) => setBindPassword(event.target.value)}
          />
        </label>
        <label className="field-label">
          База поиска для входа
          <input
            className="title-input"
            placeholder="ou=people,dc=example,dc=com"
            value={baseDn}
            onChange={(event) => setBaseDn(event.target.value)}
            autoComplete="off"
          />
        </label>
        <label className="field-label">
          Фильтр пользователя для входа
          <input
            className="title-input"
            placeholder="(mail={username})"
            value={userFilter}
            onChange={(event) => setUserFilter(event.target.value)}
            autoComplete="off"
          />
        </label>
        <label className="field-label">
          Атрибут email
          <input className="title-input" value={emailAttr} onChange={(event) => setEmailAttr(event.target.value)} />
        </label>
        <label className="field-check">
          <input type="checkbox" checked={starttls} onChange={(event) => setStarttls(event.target.checked)} />
          STARTTLS (для ldap://, не для ldaps://)
        </label>
        <label className="field-check">
          <input type="checkbox" checked={tlsVerify} onChange={(event) => setTlsVerify(event.target.checked)} />
          Проверять сертификат
        </label>
        <label className="field-label">
          Проверить поиск по email
          <input
            className="title-input"
            type="email"
            placeholder="user@example.com"
            value={probe}
            onChange={(event) => setProbe(event.target.value)}
          />
        </label>
      </div>
      <div className="composer-row">
        <button className="primary" type="submit" disabled={busy !== null}>
          {busy === "save" ? "Сохраняю…" : "Сохранить каталог"}
        </button>
        <button
          className="ghost"
          type="button"
          disabled={busy !== null}
          onClick={() => {
            setError(null);
            setNote(null);
            setBusy("test");
            void testLdap(probe.trim())
              .then((result) =>
                setNote(
                  result.entries
                    ? `Найдена одна запись${result.mail ? `: ${result.mail}` : ""}`
                    : "Служебная учётная запись подключилась. Чтение сотрудников проверяется при обновлении кэша ниже.",
                ),
              )
              .catch((err: Error) => setError(err.message))
              .finally(() => setBusy(null));
          }}
        >
          {busy === "test" ? "Проверяю…" : "Проверить LDAP"}
        </button>
      </div>
      {note && <p>{note}</p>}
      {error && <div className="banner" role="alert">{error}</div>}
    </form>
    <DirectoryCachePanel key={`${ldap?.url || ""}|${ldap?.base_dn || ""}|${ldap?.email_attr || ""}|${ldap?.configured}`} />
    </>
  );
}
