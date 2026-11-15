import { useEffect, useState } from "react";
import Landing from "./Landing.jsx";

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
  const [landingMessage, setLandingMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState("layers");
  const [landing, setLanding] = useState(null);

  async function refreshDates(prefer) {
    const rows = await request("/api/dates");
    setDates(rows);
    const target = prefer || detail?.biz_date || rows[0]?.biz_date;
    if (target) setDetail(await request(`/api/dates/${target}`));
  }

  async function refreshLanding() {
    setLanding(await request("/api/landing"));
  }

  useEffect(() => {
    if (!profile) return;
    Promise.all([refreshDates(), refreshLanding()]).catch(() => {
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
      await refreshDates();
      await refreshLanding();
    } catch (error) {
      setFormError(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function lockSetting(payload) {
    setBusy(true);
    setLandingMessage("");
    try {
      setLanding(
        await request("/api/landing/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        })
      );
      setLandingMessage(`业务日 ${payload.biz_date} 的落批办法已写定收束，当日禁止改口`);
      await refreshDates();
    } catch (error) {
      setLandingMessage(error.message);
      await refreshLanding();
    } finally {
      setBusy(false);
    }
  }

  async function orchestrate(payload) {
    setBusy(true);
    setLandingMessage("");
    try {
      const data = await request("/api/landing/orchestrate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setLanding(data);
      setLandingMessage("落批完成，落批列表、失败详情、落批设置、死信队列已同轮核齐");
      await refreshDates();
    } catch (error) {
      setLandingMessage(error.message);
      await refreshLanding();
    } finally {
      setBusy(false);
    }
  }

  function logout() {
    localStorage.removeItem("lake.token");
    localStorage.removeItem("lake.profile");
    setProfile(null);
    setDetail(null);
    setLanding(null);
  }

  if (!profile) {
    return (
      <main className="sheet gate">
        <p className="kicker">贴源层 → 明细层 · 落批台</p>
        <h1>闸口事件湖仓台</h1>
        <p>扫描原文先落贴源。失败批次按写定的落批办法收口，只有字典认得出的事件才收进明细。</p>
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
          <p className="kicker">闸口扫描 · 分层 · 落批</p>
          <h1>闸口事件湖仓台</h1>
        </div>
        <div className="who">
          <nav className="views">
            <button className={view === "layers" ? "line on" : "line"} onClick={() => setView("layers")}>
              分层台
            </button>
            <button className={view === "landing" ? "line on" : "line"} onClick={() => setView("landing")}>
              落批台
            </button>
          </nav>
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
              {row.pending_batches > 0 ? (
                <span className="pending-pill">待落批 {row.pending_batches} 批</span>
              ) : null}
            </button>
          ))}
        </aside>
        {view === "landing" ? (
          <Landing
            data={landing}
            profile={profile}
            busy={busy}
            message={landingMessage}
            onLock={lockSetting}
            onOrchestrate={orchestrate}
          />
        ) : detail ? (
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
                      <span>{row.terminal_code}{row.batch_no ? ` · 批次 ${row.batch_no}` : " · 空批次号"}</span>
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
                <h3>剔除（空批次号散扫）</h3>
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
