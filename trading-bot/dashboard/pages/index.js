import { useState, useEffect, useCallback, useMemo } from 'react';
import Head from 'next/head';
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer
} from 'recharts';

const API = 'http://localhost:8000';

function useFetch(url, interval = 15000) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    fetch(url, { cache: 'no-store' })
      .then(r => r.json())
      .then(d => { setData(d); setLoading(false); })
      .catch(() => { setData(null); setLoading(false); });
  }, [url]);

  useEffect(() => {
    load();
    const id = setInterval(load, interval);
    return () => clearInterval(id);
  }, [load, interval]);

  return { data, loading, reload: load };
}

function StatCard({ label, value, sub, color, prefix = '' }) {
  const cls = color === 'green' ? 'stat-card green-glow' : color === 'red' ? 'stat-card red-glow' : 'stat-card blue-glow';
  return (
    <div className={`card ${cls}`}>
      <div className="label">{label}</div>
      <div className="value mono" style={{ color: color === 'green' ? 'var(--accent-green)' : color === 'red' ? 'var(--accent-red)' : 'var(--accent-blue)' }}>
        {prefix}{value}
      </div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}

function ScoreBar({ score }) {
  const color = score >= 80 ? 'var(--accent-green)' : score >= 65 ? 'var(--accent-yellow)' : 'var(--accent-red)';
  return (
    <div className="score-bar-wrap">
      <div className="score-bar">
        <div className="score-bar-fill" style={{ width: `${score}%`, background: color }} />
      </div>
      <span className="score-label mono" style={{ color }}>{score}</span>
    </div>
  );
}

function fmtPrice(v) {
  const n = Number(v);
  return Number.isFinite(n) ? `$${n.toFixed(4)}` : '—';
}

function fmtPnl(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  return `${n >= 0 ? '+' : ''}$${n.toFixed(2)}`;
}

function fmtDateTime(v) {
  if (!v) return '—';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return String(v);
  return d.toLocaleString();
}

const CustomTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null;
  return (
    <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border)', padding: '10px 14px', borderRadius: 10, fontSize: 12 }}>
      <div style={{ color: 'var(--text-secondary)', marginBottom: 4 }}>{label}</div>
      <div style={{ color: 'var(--accent-cyan)', fontWeight: 700 }}>
        ${payload[0].value?.toFixed(2)}
      </div>
    </div>
  );
};

export default function Home() {
  const [dashboardMode, setDashboardMode] = useState('paper');
  const modeQuery = `mode=${dashboardMode}`;
  const { data: stats }  = useFetch(`${API}/api/stats?${modeQuery}`, 15000);
  const { data: trades } = useFetch(`${API}/api/trades?limit=200&${modeQuery}`, 15000);
  const { data: open }   = useFetch(`${API}/api/open?${modeQuery}`, 8000);
  const { data: curve }  = useFetch(`${API}/api/pnl-curve?${modeQuery}`, 30000);
  const { data: quality } = useFetch(`${API}/api/quality?${modeQuery}`, 30000);
  const { data: runtime } = useFetch(`${API}/api/runtime`, 8000);
  const { data: weightFeedback } = useFetch(`${API}/api/weight-feedback`, 30000);
  const [now, setNow]    = useState('');

  useEffect(() => {
    const tick = () => setNow(new Date().toLocaleTimeString());
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  const pnlColor = stats?.total_pnl >= 0 ? 'green' : 'red';
  const wrColor  = (stats?.win_rate ?? 0) >= 60 ? 'green' : 'red';
  const runtimeHealthScore = Number(runtime?.health?.score ?? 0);
  const runtimeHealthColor = runtimeHealthScore >= 80 ? 'green' : runtimeHealthScore >= 60 ? 'blue' : 'red';
  const apiErrorRate = Number(runtime?.health?.api_error_rate_pct ?? 0);
  const rejectRate = Number(runtime?.health?.order_reject_rate_pct ?? 0);
  const queueDrops = Number(runtime?.health?.signals_dropped_queue_full ?? 0);
  const filteredTrades = useMemo(() => trades || [], [trades]);
  const feedbackEnabled = Boolean(weightFeedback?.enabled);
  const feedbackHasReport = Boolean(weightFeedback?.has_report);
  const feedbackChanges = Number(weightFeedback?.changed_weights ?? 0);
  const feedbackClosedTrades = Number(weightFeedback?.closed_trades ?? weightFeedback?.last_auto_adjust_closed_count ?? 0);
  const feedbackColor = !feedbackEnabled ? 'red' : (feedbackHasReport ? 'green' : 'blue');
  const lastFeedbackAt = fmtDateTime(weightFeedback?.generated_at || weightFeedback?.meta_updated_at);

  return (
    <>
      <Head>
        <title>TradingBot · Dashboard</title>
        <meta name="description" content="AI-powered Binance trading bot dashboard" />
      </Head>

      {/* ── Navbar ── */}
      <nav className="navbar">
        <div className="navbar-brand">
          <div className="dot" />
          TradingBot · Binance
        </div>
        <div style={{ display: 'inline-flex', gap: 8 }}>
          {['paper', 'live'].map((mode) => {
            const active = dashboardMode === mode;
            return (
              <button
                key={mode}
                type="button"
                onClick={() => setDashboardMode(mode)}
                style={{
                  border: `1px solid ${active ? 'var(--accent-blue)' : 'var(--border)'}`,
                  background: active ? 'rgba(96,165,250,0.16)' : 'rgba(99,179,237,0.04)',
                  color: active ? 'var(--accent-blue)' : 'var(--text-secondary)',
                  borderRadius: 999,
                  padding: '6px 14px',
                  fontSize: 11,
                  fontWeight: 700,
                  letterSpacing: 0.6,
                  textTransform: 'uppercase',
                  cursor: 'pointer',
                }}
              >
                {mode}
              </button>
            );
          })}
        </div>
        <div className="nav-status">⚡ {dashboardMode === 'paper' ? 'Paper' : 'Live'} Mode</div>
        <span className="refresh-time">Live · {now}</span>
      </nav>

      <div className="page">

        {/* ── Stats Row ── */}
        <div className="grid grid-4">
          <StatCard label="Total Trades"  value={stats?.total ?? '—'}             color="blue" sub={`${dashboardMode.toUpperCase()} only`} />
          <StatCard label="Win Rate"      value={`${stats?.win_rate ?? '—'}%`}    color={wrColor}  sub={`${stats?.wins ?? 0}W / ${stats?.losses ?? 0}L`} />
          <StatCard label="Total PnL"     value={`${stats?.total_pnl >= 0 ? '+' : ''}${stats?.total_pnl?.toFixed(2) ?? '—'}`} prefix="$" color={pnlColor} />
          <StatCard label="Open Positions" value={open?.length ?? '—'}             color="blue" sub={`${dashboardMode.toUpperCase()} only`} />
        </div>

        <div className="grid grid-4">
          <StatCard label="Profit Factor" value={quality?.profit_factor ?? '—'} color={(quality?.profit_factor ?? 0) >= 1.5 ? 'green' : 'red'} sub={`Closed: ${quality?.sample_size ?? 0}`} />
          <StatCard label="Max Drawdown" value={`${quality?.max_drawdown_pct ?? '—'}%`} color={(quality?.max_drawdown_pct ?? 0) <= 10 ? 'green' : 'red'} />
          <StatCard label="Expectancy" value={`${quality?.expectancy_per_trade >= 0 ? '+' : ''}${quality?.expectancy_per_trade ?? '—'}`} prefix="$" color={(quality?.expectancy_per_trade ?? 0) >= 0 ? 'green' : 'red'} sub="Per closed trade" />
          <StatCard label="High-Conf Precision" value={`${quality?.high_conf_precision_pct ?? '—'}%`} color={(quality?.high_conf_precision_pct ?? 0) >= 60 ? 'green' : 'red'} sub="Score ≥ 70" />
        </div>

        <div className="grid grid-4">
          <StatCard label="Runtime Health" value={`${runtimeHealthScore || '—'}/100`} color={runtimeHealthColor} sub="Telemetry-derived" />
          <StatCard label="API Error Rate" value={`${apiErrorRate.toFixed(2)}%`} color={apiErrorRate <= 2 ? 'green' : 'red'} sub="Per scan session" />
          <StatCard label="Order Reject Rate" value={`${rejectRate.toFixed(2)}%`} color={rejectRate <= 20 ? 'green' : 'red'} sub="Opened + rejected" />
          <StatCard label="Queue Drops" value={queueDrops} color={queueDrops === 0 ? 'green' : 'red'} sub="Signal queue overflow" />
        </div>

        <div className="grid grid-4">
          <StatCard label="Weight Feedback" value={feedbackEnabled ? (feedbackHasReport ? 'ACTIVE' : 'WAITING') : 'OFF'} color={feedbackColor} sub={feedbackEnabled ? 'Auto-adjust enabled' : 'Disabled in config'} />
          <StatCard label="Last Recalibration" value={lastFeedbackAt} color={feedbackHasReport ? 'green' : 'blue'} sub="Most recent auto-adjust pass" />
          <StatCard label="Closed Trades @ Run" value={feedbackClosedTrades} color={feedbackHasReport ? 'green' : 'blue'} sub="Samples used at last run" />
          <StatCard label="Weights Changed" value={feedbackChanges} color={feedbackChanges > 0 ? 'green' : 'blue'} sub="Updated in latest run" />
        </div>

        {/* ── PnL Curve ── */}
        <div className="card">
          <div className="section-title">Capital Curve</div>
          {curve?.length > 1 ? (
            <ResponsiveContainer width="100%" height={220}>
              <AreaChart data={curve}>
                <defs>
                  <linearGradient id="cg" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%"  stopColor="#22d3ee" stopOpacity={0.3} />
                    <stop offset="95%" stopColor="#22d3ee" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="rgba(99,179,237,0.06)" />
                <XAxis dataKey="date" tick={{ fill: '#475569', fontSize: 11 }}
                  tickFormatter={v => v?.slice(11, 16)} />
                <YAxis tick={{ fill: '#475569', fontSize: 11 }}
                  tickFormatter={v => `$${v}`} />
                <Tooltip content={<CustomTooltip />} />
                <Area type="monotone" dataKey="capital"
                  stroke="#22d3ee" strokeWidth={2}
                  fill="url(#cg)" dot={false} />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <div style={{ height: 220, display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--text-muted)', fontSize: 13 }}>
              No closed trades yet for {dashboardMode.toUpperCase()} mode.
            </div>
          )}
        </div>

        {/* ── Open Positions ── */}
        <div className="card">
          <div className="section-title">Open Positions</div>
          {open?.length ? (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Mode</th><th>Symbol</th><th>Dir</th><th>Entry</th>
                    <th>SL</th><th>TP1</th><th>TP2</th><th>Score</th>
                  </tr>
                </thead>
                <tbody>
                  {open.map(t => (
                    <tr key={t.id}>
                      <td><span className="badge badge-open">{(t.mode || 'paper').toUpperCase()}</span></td>
                      <td>{t.symbol}</td>
                      <td><span className={`badge badge-${t.direction}`}>{t.direction.toUpperCase()}</span></td>
                      <td className="mono">{fmtPrice(t.entry_price)}</td>
                      <td className="mono pnl-neg">{fmtPrice(t.stop_loss)}</td>
                      <td className="mono pnl-pos">{fmtPrice(t.tp1)}</td>
                      <td className="mono pnl-pos">{fmtPrice(t.tp2)}</td>
                      <td><ScoreBar score={typeof t.score === 'number' ? t.score : 0} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div style={{ color: 'var(--text-muted)', fontSize: 13, textAlign: 'center', padding: '40px 0' }}>
              No open positions
            </div>
          )}
        </div>

        {/* ── Recent Trades ── */}
        <div className="card">
          <div className="section-title">Trade History</div>
          <div className="table-wrap">
            {filteredTrades.length ? (
              <table>
                <thead>
                  <tr>
                    <th>Mode</th><th>Symbol</th><th>Dir</th><th>Entry</th>
                    <th>Exit</th><th>PnL</th><th>Closed At</th><th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredTrades.slice(0, 20).map(t => (
                    <tr key={t.id}>
                      <td><span className="badge badge-open">{(t.mode || 'paper').toUpperCase()}</span></td>
                      <td>{t.symbol}</td>
                      <td><span className={`badge badge-${t.direction}`}>{t.direction.toUpperCase()}</span></td>
                      <td className="mono">{fmtPrice(t.entry_price)}</td>
                      <td className="mono">{t.exit_price != null ? fmtPrice(t.exit_price) : <span className="badge badge-open">CLOSED</span>}</td>
                      <td className={Number(t.pnl) >= 0 ? 'mono pnl-pos' : 'mono pnl-neg'}>
                        {fmtPnl(t.pnl)}
                      </td>
                      <td className="mono">{fmtDateTime(t.close_time || t.open_time)}</td>
                      <td style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                        {(t.reason || t.status || '').replace(/_/g, ' ')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div style={{ color: 'var(--text-muted)', fontSize: 13, textAlign: 'center', padding: '40px 0' }}>
                No trades yet for {dashboardMode.toUpperCase()} mode. Bot is scanning markets…
              </div>
            )}
          </div>
        </div>

        {/* ── Risk Rules Panel ── */}
        <div className="card">
          <div className="section-title">Active Risk Rules</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: 12 }}>
            {[
              ['Max Risk / Trade', '2% of capital'],
              ['Stop Loss', 'ATR × 2 + S/R level'],
              ['Trailing SL', '1.5% below peak'],
              ['TP1 (50% exit)', '1× Risk'],
              ['TP2 (full exit)', '2× Risk + Fib ext.'],
              ['Max Open Trades', '5 (diversified)'],
              ['Max / Single Trade', '20% of capital'],
              ['Daily Loss Limit', '6% → halt trading'],
              ['Max Drawdown', '15% → halt trading'],
              ['Min Signal Score', '70 / 100'],
              ['Multi-TF Confirm', '1H + 4H alignment'],
              ['Volume Filter', 'Spike > 1.5× avg'],
            ].map(([rule, val]) => (
              <div key={rule} style={{ background: 'rgba(99,179,237,0.04)', border: '1px solid var(--border)', borderRadius: 10, padding: '12px 14px' }}>
                <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginBottom: 4 }}>{rule}</div>
                <div style={{ fontSize: 13, fontWeight: 600, color: 'var(--accent-cyan)' }}>{val}</div>
              </div>
            ))}
          </div>
        </div>

      </div>
    </>
  );
}
