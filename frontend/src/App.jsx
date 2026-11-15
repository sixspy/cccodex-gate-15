import { useEffect, useState } from "react";

function loadProfile() {
  try {
    return JSON.parse(localStorage.getItem("lake.profile") || "null");
  } catch {
    return null;
  }
}

async function request(path, options = {}) {
  const token = localStorage.getItem("lake.token") || "";
  const headers = { ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "请求没有完成");
  }
  return data;
}

export default function App() {
  const [profile, setProfile] = useState(loadProfile);
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("123456");
  const [loginError, setLoginError] = useState("");
  const [dates, setDates] = useState([]);
  const [detail, setDetail] = useState(null);
  const [formError, setFormError] = useState("");
  const [busy, setBusy] = useState(false);

  async function refresh(prefer) {
    const rows = await request("/api/dates");
    setDates(rows);
    const target = prefer || detail?.biz_date || rows[0]?.biz_date;
    if (target) setDetail(await request(`/api/dates/${target}`));
  }

  useEffect(() => {
    if (!profile) return;
    refresh().catch(() => {
      localStorage.removeItem("lake.token");
      localStorage.removeItem("lake.profile");
      setProfile(null);
    });
  }, [profile]);

  async function submitLogin(event) {
    event.preventDefault();
    setLoginError("");
    try {
      const data = await request("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      localStorage.setItem("lake.token", data.access_token);
      const next = { username: data.username, role: data.role, title: data.title };
      localStorage.setItem("lake.profile", JSON.stringify(next));
      setProfile(next);
    } catch (error) {
      setLoginError(error.message);
    }
  }

  async function buildDay() {
    if (!detail) return;
    setBusy(true);
    setFormError("");
    try {
      const data = await request(`/api/dates/${detail.biz_date}/build`, { method: "POST" });
      setDetail(data);
      setDates(await request("/api/dates"));
    } catch (error) {
      setFormError(error.message);
    } finally {
      setBusy(false);
    }
  }

  function logout() {
    localStorage.removeItem("lake.token");
    localStorage.removeItem("lake.profile");
    setProfile(null);
    setDetail(null);
  }

  if (!profile) {
    return (
      <main className="sheet gate">
        <p className="kicker">贴源层 → 明细层</p>
        <h1>闸口事件湖仓台</h1>
        <p>扫描原文先落贴源。只有字典认得出的事件才收进明细。</p>
        <form onSubmit={submitLogin}>
          <label>用户<input value={username} onChange={(event) => setUsername(event.target.value)} /></label>
          <label>口令<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
          <button>打开这一层</button>
          {loginError ? <p className="warn">{loginError}</p> : null}
        </form>
      </main>
    );
  }

  return (
    <>
      <header className="bar">
        <div>
          <p className="kicker">闸口扫描 · 分层</p>
          <h1>闸口事件湖仓台</h1>
        </div>
        <div className="who">
          <span>{profile.title} {profile.username}</span>
          <button className="line" onClick={logout}>离开</button>
        </div>
      </header>
      <main className="layout">
        <aside>
          {dates.map((row) => (
            <button
              key={row.biz_date}
              className={detail?.biz_date === row.biz_date ? "day on" : "day"}
              onClick={() => request(`/api/dates/${row.biz_date}`).then(setDetail)}
            >
              <strong>{row.biz_date}</strong>
              <em>{row.built ? "明细已收" : "尚未构建"}</em>
              <span>贴源 {row.ods_count} 条 · 明细 {row.dwd_count} 条</span>
            </button>
          ))}
        </aside>
        {detail ? (
          <section className="sheet">
            <div className="split-head">
              <h2>{detail.biz_date}</h2>
              <button disabled={profile.role !== "writer" || busy} onClick={buildDay}>
                按字典构建明细层
              </button>
            </div>
            <p className="note">IN 进闸，OUT 出闸，RS 倒箱。箱号要是四位字母加七位数字。</p>
            {profile.role !== "writer" ? <p className="note">核对员不能重新构建。</p> : null}
            {formError ? <p className="warn">{formError}</p> : null}
            <div className="layers">
              <article>
                <h3>贴源层</h3>
                <ul>
                  {detail.ods.map((row) => (
                    <li key={row.id}>
                      <span>{row.terminal_code}</span>
                      <code>{JSON.stringify(row.raw_payload)}</code>
                    </li>
                  ))}
                </ul>
              </article>
              <article>
                <h3>明细层</h3>
                {detail.moves.length ? (
                  <table>
                    <thead>
                      <tr><th>来源</th><th>箱号</th><th>事件</th><th>时间</th></tr>
                    </thead>
                    <tbody>
                      {detail.moves.map((row) => (
                        <tr key={`${row.source_id}-${row.event_kind}`}>
                          <td>{row.source_id}</td>
                          <td>{row.container_no}</td>
                          <td>{row.event_kind}</td>
                          <td>{row.event_at}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : <p>这一天的明细还是空的。</p>}
                <h3>剔除</h3>
                <ul>
                  {detail.rejects.map((row) => (
                    <li key={row.source_id}><span>来源 {row.source_id}</span><em>{row.reason}</em></li>
                  ))}
                  {!detail.rejects.length ? <li>没有被剔除的扫描</li> : null}
                </ul>
              </article>
            </div>
          </section>
        ) : null}
      </main>
    </>
  );
}
