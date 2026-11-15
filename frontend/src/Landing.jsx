import { useMemo, useState } from "react";

const OUTCOME_LABEL = {
  LANDED_ROLLBACK: "已落批·整批回滚",
  LANDED_PARTIAL: "已落批·部分落库",
  DRIFT: "落批办法偏了",
  CONTENTION: "落批争用",
};

const STATUS_LABEL = {
  PENDING: "待落批",
  LANDED: "已落批",
};

function strategyName(data, code) {
  const hit = (data?.strategies || []).find((item) => item.code === code);
  return hit ? hit.name : code;
}

export default function Landing({ data, profile, busy, onLock, onOrchestrate, message }) {
  const [bizDate, setBizDate] = useState("");
  const [strategy, setStrategy] = useState("ROLLBACK");

  const isKeeper = profile.role === "keeper";
  const settingsByDate = useMemo(
    () => Object.fromEntries((data?.settings || []).map((row) => [row.biz_date, row])),
    [data]
  );
  const failedBatches = (data?.batches || []).filter((row) => row.invalid_count > 0);

  return (
    <section className="sheet landing">
      <div className="split-head">
        <h2>落批台</h2>
        <div className="totals">
          <span>回滚 <strong>{data?.totals.rollback_count ?? 0}</strong></span>
          <span>先入库 <strong>{data?.totals.landed_count ?? 0}</strong></span>
          <span>死信 <strong>{data?.totals.dead_count ?? 0}</strong></span>
        </div>
      </div>
      <p className="note">
        落批办法只有两种：整批回滚一笔不进明细，或成功的先落库、失败的进死信。写定即收束，当日禁止改口。
      </p>
      {message ? <p className="warn">{message}</p> : null}

      <h3>落批设置</h3>
      <table>
        <thead>
          <tr><th>业务日</th><th>办法名</th><th>收束人</th><th>收束时间</th></tr>
        </thead>
        <tbody>
          {(data?.settings || []).map((row) => (
            <tr key={row.biz_date}>
              <td>{row.biz_date}</td>
              <td><span className="tag tag-setting">{row.strategy_name}</span></td>
              <td>{row.locked_by}</td>
              <td>{row.locked_at}</td>
            </tr>
          ))}
          {!(data?.settings || []).length ? (
            <tr><td colSpan="4">还没有收束过落批办法</td></tr>
          ) : null}
        </tbody>
      </table>

      {isKeeper ? (
        <form
          className="lock-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (!bizDate) return;
            onLock({ biz_date: bizDate, strategy });
          }}
        >
          <label>业务日
            <input
              type="date"
              value={bizDate}
              onChange={(event) => setBizDate(event.target.value)}
            />
          </label>
          <label>落批办法
            <select value={strategy} onChange={(event) => setStrategy(event.target.value)}>
              {(data?.strategies || []).map((item) => (
                <option key={item.code} value={item.code}>{item.name}</option>
              ))}
            </select>
          </label>
          <button disabled={busy || !bizDate}>写定收束</button>
        </form>
      ) : (
        <p className="note">只有仓管能收束落批办法、编排失败批次。</p>
      )}

      <h3>落批列表</h3>
      <table>
        <thead>
          <tr>
            <th>业务日</th><th>批次号</th><th>办法名</th>
            <th>回滚次数</th><th>先入库次数</th><th>死信条数</th><th>仓管</th><th>结果</th>
          </tr>
        </thead>
        <tbody>
          {(data?.records || []).map((row) => (
            <tr key={row.id} className={`outcome outcome-${row.outcome.toLowerCase()}`}>
              <td>{row.biz_date}</td>
              <td>{row.batch_no}</td>
              <td>{row.strategy_name}</td>
              <td>{row.rollback_count}</td>
              <td>{row.landed_count}</td>
              <td>{row.dead_count}</td>
              <td>{row.keeper}</td>
              <td>
                <span className={`tag tag-${row.outcome.toLowerCase()}`}>
                  {OUTCOME_LABEL[row.outcome] || row.outcome}
                </span>
              </td>
            </tr>
          ))}
          {!(data?.records || []).length ? (
            <tr><td colSpan="8">还没有落批记录</td></tr>
          ) : null}
        </tbody>
      </table>

      <h3>失败批次与编排</h3>
      <table>
        <thead>
          <tr>
            <th>业务日</th><th>批次号</th><th>扫描</th><th>合法</th><th>失败</th>
            <th>落批设置</th><th>批次状态</th><th>已落办法</th><th>编排</th>
          </tr>
        </thead>
        <tbody>
          {failedBatches.map((batch) => {
            const setting = settingsByDate[batch.biz_date];
            const locked = Boolean(setting);
            return (
              <tr key={batch.id} className={batch.status === "PENDING" ? "row-pending" : "row-landed"}>
                <td>{batch.biz_date}</td>
                <td>{batch.batch_no}</td>
                <td>{batch.total_count}</td>
                <td>{batch.valid_count}</td>
                <td>{batch.invalid_count}</td>
                <td>
                  {locked ? (
                    <span className="tag tag-setting">{setting.strategy_name}</span>
                  ) : (
                    <em>尚未收束</em>
                  )}
                </td>
                <td>
                  <span className={`tag ${batch.status === "PENDING" ? "tag-pending" : "tag-landed"}`}>
                    {STATUS_LABEL[batch.status] || batch.status}
                  </span>
                </td>
                <td>{batch.landed_strategy_name || "—"}</td>
                <td className="orchestrate">
                  {(data?.strategies || []).map((item) => {
                    const mismatch = locked && setting.strategy !== item.code;
                    return (
                      <button
                        key={item.code}
                        className="line small"
                        disabled={!isKeeper || busy || batch.status === "LANDED" || !locked}
                        title={
                          !locked
                            ? "落批办法尚未收束，不准编排"
                            : mismatch
                              ? "办法跟落批设置不合，会记办法偏了"
                              : ""
                        }
                        onClick={() =>
                          onOrchestrate({ batch_id: batch.id, strategy: item.code })
                        }
                      >
                        {item.name}
                      </button>
                    );
                  })}
                </td>
              </tr>
            );
          })}
          {!failedBatches.length ? (
            <tr><td colSpan="9">没有失败批次</td></tr>
          ) : null}
        </tbody>
      </table>

      <div className="two-col">
        <article>
          <h3>失败详情（对照整批回滚与部分落库）</h3>
          <table>
            <thead>
              <tr><th>业务日</th><th>批次</th><th>来源</th><th>原因</th><th>去向</th><th>当日办法</th></tr>
            </thead>
            <tbody>
              {(data?.failures || []).map((row) => (
                <tr
                  key={row.source_id}
                  className={
                    row.disposition === "死信队列"
                      ? "row-dlq"
                      : row.disposition === "整批回滚"
                        ? "row-rollback"
                        : ""
                  }
                >
                  <td>{row.biz_date}</td>
                  <td>{row.batch_no || <em>空批次号</em>}</td>
                  <td>{row.source_id}</td>
                  <td>{row.reason}</td>
                  <td>
                    <span
                      className={`tag ${
                        row.disposition === "死信队列"
                          ? "tag-dlq"
                          : row.disposition === "整批回滚"
                            ? "tag-rollback"
                            : "tag-pending"
                      }`}
                    >
                      {row.disposition}
                    </span>
                  </td>
                  <td>{row.setting_strategy_name || "—"}</td>
                </tr>
              ))}
              {!(data?.failures || []).length ? (
                <tr><td colSpan="6">没有失败扫描</td></tr>
              ) : null}
            </tbody>
          </table>
          <p className="note">空批次号的扫描只在剔除清单里，不进死信队列。</p>
        </article>

        <article>
          <h3>死信队列</h3>
          <table>
            <thead>
              <tr><th>业务日</th><th>批次</th><th>来源</th><th>原因</th><th>落信仓管</th><th>时间</th></tr>
            </thead>
            <tbody>
              {(data?.dead_letters || []).map((row) => (
                <tr key={row.id} className="row-dlq">
                  <td>{row.biz_date}</td>
                  <td>{row.batch_no}</td>
                  <td>{row.source_id}</td>
                  <td>{row.reason}</td>
                  <td>{row.landed_by}</td>
                  <td>{row.landed_at}</td>
                </tr>
              ))}
              {!(data?.dead_letters || []).length ? (
                <tr><td colSpan="6">死信队列还是空的</td></tr>
              ) : null}
            </tbody>
          </table>
        </article>
      </div>
    </section>
  );
}
