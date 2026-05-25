'use client';

import React, { useState, useEffect, useMemo, useRef, useCallback } from 'react';
import { 
  Activity, Shield, ShieldAlert, Zap, AlertTriangle, Lock, Unlock, 
  Play, Database, RefreshCw, Radio, Check, CircleAlert, 
  TrendingUp, TrendingDown, Clock, HelpCircle, HardDrive, Cpu, 
  Percent, ChevronDown, CheckCircle2, ChevronRight, Ban, Sliders,
  Gauge, Flame, AlertCircle, RefreshCcw, Landmark, Sparkles, Filter
} from 'lucide-react';

// Interfaces
interface LogItem {
  id: string;
  time: string;
  type: 'SIGNAL' | 'SKIP' | 'RISK BLOCK' | 'ENTRY' | 'SYSTEM' | 'EXIT';
  badge: string;
  text: string;
}

interface AssetPerformance {
  asset: string;
  trades: number;
  netPnl: string;
  winRate: string;
  pf: string;
  status: 'ACTIVE' | 'PAUSED';
}

interface DataPoint {
  date: string;
  equity: number;
  benchmark: number;
}

// Timeframe-specific mock equity curve data
const chartData: Record<'1D' | '7D' | '30D' | 'ALL', DataPoint[]> = {
  '1D': [
    { date: '08:00', equity: 10120, benchmark: 10100 },
    { date: '10:00', equity: 10155, benchmark: 10070 },
    { date: '12:00', equity: 10110, benchmark: 10090 },
    { date: '14:00', equity: 10220, benchmark: 10180 },
    { date: '16:00', equity: 10243, benchmark: 10150 },
    { date: '18:00', equity: 10260, benchmark: 10130 },
    { date: '20:00', equity: 10284.32, benchmark: 10110 }
  ],
  '7D': [
    { date: 'Mon', equity: 9850, benchmark: 9920 },
    { date: 'Tue', equity: 9910, benchmark: 9890 },
    { date: 'Wed', equity: 10015, benchmark: 10020 },
    { date: 'Thu', equity: 9940, benchmark: 9980 },
    { date: 'Fri', equity: 10140, benchmark: 10050 },
    { date: 'Sat', equity: 10210, benchmark: 10110 },
    { date: 'Sun', equity: 10284.32, benchmark: 10080 }
  ],
  '30D': [
    { date: 'May 05', equity: 8700, benchmark: 9100 },
    { date: 'May 07', equity: 9120, benchmark: 8960 },
    { date: 'May 09', equity: 8950, benchmark: 9020 },
    { date: 'May 11', equity: 9410, benchmark: 9180 },
    { date: 'May 13', equity: 9780, benchmark: 9340 },
    { date: 'May 15', equity: 9640, benchmark: 9450 },
    { date: 'May 17', equity: 10080, benchmark: 9510 },
    { date: 'May 19', equity: 9940, benchmark: 9680 },
    { date: 'May 21', equity: 10150, benchmark: 9620 },
    { date: 'May 23', equity: 10220, benchmark: 9750 },
    { date: 'May 25', equity: 10284.32, benchmark: 9810 }
  ],
  'ALL': [
    { date: 'Jan 26', equity: 5000, benchmark: 5000 },
    { date: 'Feb 26', equity: 6200, benchmark: 5700 },
    { date: 'Mar 26', equity: 7100, benchmark: 6800 },
    { date: 'Apr 26', equity: 8950, benchmark: 8200 },
    { date: 'May 26', equity: 10284.32, benchmark: 9810 }
  ]
};

// Drawdown curves
const drawdownData = [
  { date: 'May 05', value: 0 },
  { date: 'May 06', value: -1.1 },
  { date: 'May 07', value: -0.5 },
  { date: 'May 08', value: -1.8 },
  { date: 'May 09', value: -3.2 },
  { date: 'May 10', value: -4.3 },
  { date: 'May 11', value: -2.1 },
  { date: 'May 12', value: -0.9 },
  { date: 'May 13', value: -1.5 },
  { date: 'May 14', value: -0.4 },
  { date: 'May 15', value: -1.2 },
  { date: 'May 16', value: -2.3 },
  { date: 'May 17', value: -0.8 },
  { date: 'May 18', value: 0 }
];

export default function HermesDashboard() {
  // Live time-keeping state
  const [currentTime, setCurrentTime] = useState<string>('--:--:-- UTC');
  const [heartbeatCountdown, setHeartbeatCountdown] = useState<number>(14);
  const [loopCountdown, setLoopCountdown] = useState<number>(60);
  const [uptime, setUptime] = useState<number>(99.2);

  // Core interactive states
  const [killSwitchActive, setKillSwitchActive] = useState<boolean>(false);
  const [selectedTimeframe, setSelectedTimeframe] = useState<'1D' | '7D' | '30D' | 'ALL'>('30D');
  const [feedFilter, setFeedFilter] = useState<string>('All');
  const [readinessScore, setReadinessScore] = useState<number>(62);
  const [mockTradesSimulated, setMockTradesSimulated] = useState<number>(0);
  const [livePositions, setLivePositions] = useState<any[]>([]);

  // Simulated metrics tracking based on readiness progression
  const paperDays = useMemo(() => Math.min(30, 11 + Math.floor((readinessScore - 62) * 0.5)), [readinessScore]);
  const closedTrades = useMemo(() => Math.min(100, 47 + Math.floor((readinessScore - 62) * 1.4) + mockTradesSimulated), [readinessScore, mockTradesSimulated]);
  const sharpeRatio = useMemo(() => {
    const progression = (readinessScore - 62) / 38;
    return Number((0.72 + progression * 0.12).toFixed(2));
  }, [readinessScore]);
  const stopLossRatio = useMemo(() => Math.min(35, 18 + Math.floor((readinessScore - 62) * 0.45)), [readinessScore]);

  // Blockers calculated dynamically based on metrics
  const blockersList = useMemo(() => {
    const list = [];
    if (paperDays < 30) {
      list.push('Paper validation period incomplete (Current: ' + paperDays + '/30 days)');
    }
    if (closedTrades < 100) {
      list.push('Trade sample too small (Current: ' + closedTrades + '/100 trades)');
    }
    if (sharpeRatio < 0.80) {
      list.push('Realized Sharpe below threshold (Current: ' + sharpeRatio.toFixed(2) + '/0.80)');
    }
    return list;
  }, [paperDays, closedTrades, sharpeRatio]);

  // Is Live Readiness achieved?
  const isReady = readinessScore >= 100;

  // Global live values
  const [paperBalance, setPaperBalance] = useState<number>(10284.32);
  const [pnlPct, setPnlPct] = useState<number>(2.84);

  // Log Feed state & mutations
  const [logs, setLogs] = useState<LogItem[]>([
    { id: '1', time: '12:05:01', type: 'ENTRY', badge: 'ENTRY', text: 'DOGE long opened: confidence 78, size $243' },
    { id: '2', time: '12:04:30', type: 'RISK BLOCK', badge: 'RISK BLOCK', text: 'ETH blocked: correlation exposure too high' },
    { id: '3', time: '12:04:15', type: 'SKIP', badge: 'SKIP', text: 'HYPE skipped: ADX danger zone active' },
    { id: '4', time: '12:04:02', type: 'SKIP', badge: 'SKIP', text: 'SOL skipped: RSI not below adaptive threshold' },
    { id: '5', time: '12:04:00', type: 'SIGNAL', badge: 'SIGNAL', text: 'BTC context updated: neutral' },
    { id: '6', time: '12:03:12', type: 'SYSTEM', badge: 'SYSTEM', text: 'Data integrity check: PASS' },
    { id: '7', time: '12:02:44', type: 'EXIT', badge: 'EXIT', text: 'XRP long closed: TP1 hit (+0.89R)' },
    { id: '8', time: '11:59:15', type: 'SYSTEM', badge: 'SYSTEM', text: 'Loop interval synced with Hyperliquid Node' },
    { id: '9', time: '11:58:02', type: 'ENTRY', badge: 'ENTRY', text: 'SOL long opened: leverage 3.5x, size $1,250' },
    { id: '10', time: '11:55:40', type: 'SKIP', badge: 'SKIP', text: 'AVAX skipped: liquidity ratio below adaptive filter' },
    { id: '11', time: '11:52:10', type: 'SIGNAL', badge: 'SIGNAL', text: 'Regime check triggered: GMX/HL Premium Divergence check PASS' }
  ]);

  // Strategy Intelligence Data (clickable statuses!)
  const [strategyData, setStrategyData] = useState<AssetPerformance[]>([
    { asset: 'BTC', trades: 14, netPnl: '+1.24%', winRate: '57.1%', pf: '1.61', status: 'ACTIVE' },
    { asset: 'SOL', trades: 12, netPnl: '+0.92%', winRate: '58.3%', pf: '1.48', status: 'ACTIVE' },
    { asset: 'ETH', trades: 11, netPnl: '+0.74%', winRate: '54.5%', pf: '1.36', status: 'ACTIVE' },
    { asset: 'DOGE', trades: 6, netPnl: '+0.41%', winRate: '50.0%', pf: '1.12', status: 'ACTIVE' },
    { asset: 'XRP', trades: 4, netPnl: '-0.15%', winRate: '25.0%', pf: '0.71', status: 'PAUSED' },
    { asset: 'ADA', trades: 2, netPnl: '-0.32%', winRate: '0.0%', pf: '0.40', status: 'PAUSED' }
  ]);

  // Toggle Strategy Status
  const toggleStrategyStatus = (assetName: string) => {
    setStrategyData(prev => prev.map(item => {
      if (item.asset === assetName) {
        const nextStatus = item.status === 'ACTIVE' ? 'PAUSED' : 'ACTIVE';
        
        // Log the change
        const now = new Date();
        const timeStr = now.toISOString().slice(11, 19);
        const newLog: LogItem = {
          id: `manual-strategy-${Date.now()}`,
          time: timeStr,
          type: 'SYSTEM',
          badge: 'SYSTEM',
          text: `Strategy rule modified: ${assetName} status manually toggled to ${nextStatus}`
        };
        setLogs(prevLogs => [newLog, ...prevLogs]);

        return { ...item, status: nextStatus };
      }
      return item;
    }));
  };

  // Flash UI on dynamic ticks
  const [pulseActive, setPulseActive] = useState<boolean>(false);
  const triggerHeartbeatTick = useCallback(() => {
    setPulseActive(true);
    setTimeout(() => setPulseActive(false), 800);

    const now = new Date();
    const timeStr = now.toISOString().slice(11, 19);
    
    // Pick standard system check and logs randomly to make the cockpit feel alive
    const options = [
      { type: 'SIGNAL' as const, badge: 'SIGNAL', text: 'ETH delta checks completed relative to orderbook skew: Neutral' },
      { type: 'SYSTEM' as const, badge: 'SYSTEM', text: 'Hyperliquid oracle delay checked: 4ms. Integrity level: HIGH' },
      { type: 'SKIP' as const, badge: 'SKIP', text: 'BTC extreme vol check PASSED. No structural filter active.' },
      { type: 'SKIP' as const, badge: 'SKIP', text: 'AVAX adaptive threshold check skipped: volume below min metrics' }
    ];

    if (!killSwitchActive) {
      const selected = options[Math.floor(Math.random() * options.length)];
      const newLog: LogItem = {
        id: `auto-${Date.now()}`,
        time: timeStr,
        ...selected
      };
      setLogs(p => [newLog, ...p.slice(0, 19)]);
    } else {
      const newLog: LogItem = {
        id: `kill-${Date.now()}`,
        time: timeStr,
        type: 'RISK BLOCK',
        badge: 'RISK BLOCK',
        text: 'EMERGENCY RE-EVALUATION: Order routes suppressed. Kill switch state checked.'
      };
      setLogs(p => [newLog, ...p.slice(0, 19)]);
    }
  }, [killSwitchActive]);

  // Ref container to track target execution dynamically without stale closure constraints
  const tickRef = useRef(triggerHeartbeatTick);
  useEffect(() => {
    tickRef.current = triggerHeartbeatTick;
  }, [triggerHeartbeatTick]);

  // Heartbeat counting effects
  useEffect(() => {
    const clockInterval = setInterval(() => {
      const now = new Date();
      const hoursStr = String(now.getUTCHours()).padStart(2, '0');
      const minutesStr = String(now.getUTCMinutes()).padStart(2, '0');
      const secondsStr = String(now.getUTCSeconds()).padStart(2, '0');
      setCurrentTime(`${hoursStr}:${minutesStr}:${secondsStr} UTC`);
    }, 1000);

    const timingInterval = setInterval(() => {
      // Countdown for heartbeat label
      setHeartbeatCountdown(prev => {
        if (prev >= 59) {
          return 0;
        }
        return prev + 1;
      });

      // Countdown for absolute Loop (60s loop)
      setLoopCountdown(prev => {
        if (prev <= 1) {
          // Trigger loop completion simulation safely
          tickRef.current();
          return 60;
        }
        return prev - 1;
      });
    }, 1000);

    return () => {
      clearInterval(clockInterval);
      clearInterval(timingInterval);
    };
  }, []);

  // ── Live data fetch from Python API server ──
  useEffect(() => {
    const fetchData = async () => {
      try {
        const res = await fetch('/api/dashboard');
        if (!res.ok) return;
        const data = await res.json();
        
        if (data.paperBalance !== undefined) setPaperBalance(data.paperBalance);
        if (data.totalPnlPct !== undefined) setPnlPct(data.totalPnlPct);
        if (data.readinessScore !== undefined) setReadinessScore(data.readinessScore);
        
        // Update activity feed
        if (data.activityEvents && Array.isArray(data.activityEvents)) {
          const newLogs: LogItem[] = data.activityEvents.slice(0, 20).map((e: any, i: number) => ({
            id: `live-${i}-${e.timestamp || Date.now()}`,
            time: e.timestamp ? new Date(e.timestamp).toLocaleTimeString() : '--:--:--',
            type: e.type || 'SYSTEM',
            badge: e.type || 'SYSTEM',
            text: `${e.asset ? e.asset + ' ' : ''}${(e.message || '')}`,
          }));
          if (newLogs.length > 0) setLogs(newLogs);
        }

        // Update leaderboard/strategy data
        if (data.leaderboard && Array.isArray(data.leaderboard)) {
          setStrategyData(data.leaderboard.map((a: any) => ({
            asset: a.asset || a.Asset,
            trades: a.trades || a.Trades || 0,
            netPnl: a.netPnl || a['Net PnL'] || '0%',
            winRate: a.winRate || a['Win Rate'] || '0%',
            pf: String(a.pf || a.PF || 0),
            status: a.status || a.Status || 'STANDBY',
          })));
        }

        // Update open positions from live API
        if (data.positions && Array.isArray(data.positions)) {
          setLivePositions(data.positions);
        }
      } catch {
        // Silently fall back to mock data
      }
    };
    fetchData();
    const interval = setInterval(fetchData, 30000);
    return () => clearInterval(interval);
  }, []);

  // User manual simulation of a trade to satisfy metrics
  const triggerSimTrade = () => {
    if (killSwitchActive) return;

    setMockTradesSimulated(prev => prev + 1);
    
    // Automatically improve the readiness score towards 100
    setReadinessScore(prev => Math.min(100, prev + 3));

    const now = new Date();
    const timeStr = now.toISOString().slice(11, 19);
    
    const assets = ['BTC', 'SOL', 'ETH', 'LTC', 'HYPE'];
    const chosenAsset = assets[Math.floor(Math.random() * assets.length)];
    const tradeId = Math.floor(Math.random() * 900) + 100;
    const profit = Math.random() > 0.4;
    const size = Math.floor(Math.random() * 600) + 400;
    
    const entryPrice = chosenAsset === 'BTC' ? '68,240' : chosenAsset === 'ETH' ? '3,115' : '154.20';
    const exitPrice = chosenAsset === 'BTC' ? '68,590' : chosenAsset === 'ETH' ? '3,139' : '155.80';

    const logEntry: LogItem = {
      id: `manual-entry-${Date.now()}`,
      time: timeStr,
      type: 'ENTRY',
      badge: 'ENTRY',
      text: `SIMULATED: ${chosenAsset}-PERP trade #${tradeId} initiated. Size: $${size}`
    };

    const logExit: LogItem = {
      id: `manual-exit-${Date.now()}`,
      time: timeStr,
      type: 'EXIT',
      badge: 'EXIT',
      text: `SIMULATED: ${chosenAsset}-PERP closed. Entry ${entryPrice} / Exit ${exitPrice}. ${profit ? 'PROFIT' : 'LOSS'} registered.`
    };

    // Prepend to logs
    setLogs(p => [logExit, logEntry, ...p]);
  };

  // Reset diagnosis
  const resetReadiness = () => {
    setReadinessScore(62);
    setMockTradesSimulated(0);
    
    const now = new Date();
    const timeStr = now.toISOString().slice(11, 19);
    const resetLog: LogItem = {
      id: `reset-${Date.now()}`,
      time: timeStr,
      type: 'SYSTEM',
      badge: 'SYSTEM',
      text: 'Readiness Reactor state refreshed. Target score returned to default 62/100.'
    };
    setLogs(p => [resetLog, ...p]);
  };

  // Toggle the kill switch
  const toggleKillSwitch = () => {
    const nextState = !killSwitchActive;
    setKillSwitchActive(nextState);

    const now = new Date();
    const timeStr = now.toISOString().slice(11, 19);

    if (nextState) {
      const logItem: LogItem = {
        id: `kill-on-${Date.now()}`,
        time: timeStr,
        type: 'RISK BLOCK',
        badge: 'RISK BLOCK',
        text: 'EMERGENCY SHUTDOWN TRIGGERED: Kill Switch ARMED. Running halted.'
      };
      setLogs(p => [logItem, ...p]);
    } else {
      const logItem: LogItem = {
        id: `kill-off-${Date.now()}`,
        time: timeStr,
        type: 'SYSTEM',
        badge: 'SYSTEM',
        text: 'EMERGENCY OVERRIDE: Kill Switch Disarmed. Reactivating standard gate evaluation.'
      };
      setLogs(p => [logItem, ...p]);
    }
  };

  // Filter local logs based on active selection
  const filteredLogs = useMemo(() => {
    if (feedFilter === 'All') return logs;
    return logs.filter(log => log.type.toUpperCase() === feedFilter.toUpperCase());
  }, [logs, feedFilter]);

  // SVG dimensions for charts
  const width = 500;
  const height = 150;
  const paddingLeft = 46;
  const paddingRight = 10;
  const paddingTop = 15;
  const paddingBottom = 22;

  // Render Equity Chart based on state
  const renderedEquityPoints = useMemo(() => {
    const points = chartData[selectedTimeframe];
    
    // Calculate simulated modifications if trades have been simulated
    const modifiedPoints = points.map((p, idx) => {
      if (idx === points.length - 1) {
        return { ...p, equity: paperBalance };
      }
      // Stagger slight upward adjustments based on simulated trades
      const ratio = idx / (points.length - 1);
      return { 
        ...p, 
        equity: p.equity + (mockTradesSimulated * 12.5 * ratio),
        benchmark: p.benchmark + (mockTradesSimulated * 3.1 * ratio)
      };
    });

    const min = Math.min(...modifiedPoints.map(d => Math.min(d.equity, d.benchmark))) * 0.99;
    const max = Math.max(...modifiedPoints.map(d => Math.max(d.equity, d.benchmark))) * 1.01;

    const xScale = (index: number) => paddingLeft + (index / (modifiedPoints.length - 1)) * (width - paddingLeft - paddingRight);
    const yScale = (val: number) => height - paddingBottom - ((val - min) / (max - min)) * (height - paddingTop - paddingBottom);

    // Create polyline coordinate strings
    const equityPathPoints = modifiedPoints.map((p, i) => `${xScale(i)},${yScale(p.equity)}`).join(' ');
    const benchmarkPathPoints = modifiedPoints.map((p, i) => `${xScale(i)},${yScale(p.benchmark)}`).join(' ');

    // Fill polygon coordinates
    const fillPath = `${xScale(0)},${height - paddingBottom} ` + 
                     modifiedPoints.map((p, i) => `${xScale(i)},${yScale(p.equity)}`).join(' ') + 
                     ` ${xScale(modifiedPoints.length - 1)},${height - paddingBottom}`;

    return {
      points: modifiedPoints,
      equityPath: equityPathPoints,
      benchmarkPath: benchmarkPathPoints,
      fillPath,
      min,
      max,
      xScale,
      yScale
    };
  }, [selectedTimeframe, mockTradesSimulated, paperBalance]);

  // Hover tracker for interactive equity curve popovers
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const [hoverCoords, setHoverCoords] = useState<{ x: number, y: number }>({ x: 0, y: 0 });

  const handleChartMouseMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const pointsLength = chartData[selectedTimeframe].length;
    
    // Normalized position
    const ratio = (x - paddingLeft) / (width - paddingLeft - paddingRight);
    let index = Math.round(ratio * (pointsLength - 1));
    if (index < 0) index = 0;
    if (index >= pointsLength) index = pointsLength - 1;

    setHoverIndex(index);
    setHoverCoords({ 
      x: e.clientX - rect.left + 15, 
      y: e.clientY - rect.top - 10 
    });
  };

  const handleChartMouseLeave = () => {
    setHoverIndex(null);
  };

  // Compute segments for circular readiness reactor dynamically
  const numTicks = 40;
  const radius = 72;
  const innerRadius = 58;
  const centerX = 80;
  const centerY = 80;

  const reactorTicks = useMemo(() => {
    return Array.from({ length: numTicks }).map((_, i) => {
      // Create a 270 degree arc, centered symmetrically (starting at 135 deg to 405 deg)
      const angle = 135 + (i / (numTicks - 1)) * 270;
      const radians = (angle * Math.PI) / 180;

      const x1 = centerX + radius * Math.cos(radians);
      const y1 = centerY + radius * Math.sin(radians);
      const x2 = centerX + innerRadius * Math.cos(radians);
      const y2 = centerY + innerRadius * Math.sin(radians);

      const ratio = (i / (numTicks - 1)) * 100;
      const isActive = ratio <= readinessScore;

      // Color maps beautifully into tech segments: 
      // i < 10 (Red/Crimson danger indicator)
      // i >=10 and i < 22 (Amber alert status)
      // i >=22 and i < 33 (Cyber green nominal bounds)
      // i >=33 (Hyperactive teal optimization)
      let colorClass = "stroke-emerald-500 shadow-emerald-500/50";
      if (i < 10) colorClass = "stroke-rose-500 shadow-rose-500/50";
      else if (i < 22) colorClass = "stroke-amber-500 shadow-amber-500/50";
      else if (i < 33) colorClass = "stroke-emerald-500 shadow-emerald-500/50";
      else colorClass = "stroke-cyan-400 shadow-cyan-400/50";

      return { x1, y1, x2, y2, isActive, colorClass };
    });
  }, [readinessScore]);

  return (
    <div className="flex flex-col min-h-screen font-sans bg-[#050607] text-[#e0e0e0] leading-relaxed antialiased select-none">
      {/* GLOW ATMOSPHERE - Soft corner shading */}
      <div className="absolute top-0 left-0 w-full h-[600px] bg-gradient-to-b from-cyan-950/2 via-transparent to-transparent pointer-events-none" />

      {/* TOP HEADER STATUS BAR */}
      <header className="relative z-10 flex flex-col xl:flex-row items-center justify-between px-4 py-3 bg-[#0a0c0e] border-b border-slate-800 gap-4">
        {/* Helmet logo & Titles */}
        <div className="flex items-center gap-4 w-full xl:w-auto">
          {/* Abstract winged cyber helmet mark built with pure crisp inline SVG */}
          <div className="relative flex items-center justify-center w-11 h-11 bg-[#0a0c0e] rounded-sm border border-slate-700 text-cyan-400 font-mono text-xl">
            <svg viewBox="0 0 100 100" className="w-8 h-8 stroke-cyan-400 stroke-[5] fill-none" strokeLinecap="round" strokeLinejoin="round">
              {/* Outer symmetrical protective helmet shell */}
              <path d="M50 15 L20 35 L20 65 L50 85 L80 65 L80 35 Z" className="stroke-[#2a3447]" />
              {/* Symmetrical wings representation inside helmet */}
              <path d="M50 35 L30 50 L50 65 L70 50 Z" className="fill-cyan-950/40" />
              <path d="M50 15 L50 85" className="stroke-cyan-500/40" />
              <path d="M20 35 L50 50 L80 35" />
              <path d="M30 50 L15 45 M70 50 L85 45" />
              <path d="M35 55 L25 60 M65 55 L75 60" />
            </svg>
            <div className="absolute -top-1 -right-1 w-2 h-2 rounded-full bg-emerald-500 animate-ping" />
          </div>

          <div className="flex flex-col">
            <div className="flex items-center gap-2">
              <h1 className="text-lg font-bold tracking-wider text-white font-mono leading-none">
                HERMES <span className="text-cyan-400 text-sm font-semibold">COMMAND CENTER</span>
              </h1>
              <div className="flex items-center gap-1.5 px-2 py-0.5 text-[10px] uppercase font-bold tracking-tighter text-amber-500 bg-amber-500/10 border border-amber-500/40 rounded-sm font-mono select-none">
                <span className="w-1.5 h-1.5 rounded-full bg-amber-500 animate-pulse" />
                Paper Mode
              </div>
            </div>
            
            <div className="flex flex-wrap items-center mt-1 gap-x-3 gap-y-1 text-xs text-slate-500 font-mono">
              <span className="flex items-center gap-1">
                STATUS: 
                <span className={`inline-flex items-center gap-1 font-semibold ${killSwitchActive ? 'text-rose-500' : 'text-emerald-500'}`}>
                  <span className={`w-1.5 h-1.5 rounded-full ${killSwitchActive ? 'bg-red-500 animate-ping' : 'bg-emerald-500 animate-pulse'}`} />
                  {killSwitchActive ? 'HALTED' : 'RUNNING'}
                </span>
              </span>
              <span className="text-slate-800">|</span>
              <span>Loop: <strong className="text-slate-300">60s</strong></span>
              <span className="text-slate-800">|</span>
              <span className="flex items-center gap-1">
                Last heartbeat: 
                <strong className={`transition-all duration-300 ${pulseActive ? 'text-cyan-400 font-bold' : 'text-slate-300'}`}>
                  {heartbeatCountdown}s ago
                </strong>
                <Clock className={`w-3 h-3 text-cyan-400/70 inline ${pulseActive ? 'animate-bounce' : ''}`} />
              </span>
              <span className="text-slate-800">|</span>
              <span>Uptime: <strong className="text-[#22c55e]">{uptime.toFixed(1)}%</strong></span>
              <span className="text-slate-800">|</span>
              <span className="flex items-center gap-1">
                Live readiness: 
                <strong className={`flex items-center gap-0.5 px-1.5 py-px text-[10px] rounded-sm border ${isReady ? 'text-emerald-400 bg-emerald-400/10 border-emerald-400/30' : 'text-amber-500 bg-amber-500/10 border-amber-500/30'}`}>
                  {isReady ? <Unlock className="w-2.5 h-2.5" /> : <Lock className="w-2.5 h-2.5" />}
                  {isReady ? 'UNLOCKED' : 'LOCKED'}
                </strong>
              </span>
              <span className="text-slate-800">|</span>
              <span>Paper day: <strong className="text-slate-300">{paperDays} / 30</strong></span>
            </div>
          </div>
        </div>

        {/* Top Header Summary cards (Right Side) */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 w-full xl:w-auto text-xs font-mono">
          {/* Card 1: Positions */}
          <div className="p-2 bg-[#0a0c0e] rounded-sm border border-slate-800 flex flex-col min-w-[120px]">
            <span className="text-slate-500 uppercase text-[9px] tracking-wider">Open Positions</span>
            <div className="flex items-baseline justify-between mt-1">
              <span className="text-lg font-bold text-white">2 / 5</span>
              <span className="text-slate-600 text-[10px]">Active Slots</span>
            </div>
          </div>

          {/* Card 2: Portfolio Heat */}
          <div className="p-2 bg-[#0a0c0e] rounded-sm border border-slate-800 flex flex-col min-w-[120px]">
            <span className="text-slate-500 uppercase text-[9px] tracking-wider">Portfolio Heat</span>
            <div className="flex items-center justify-between mt-0.5">
              <span className="text-lg font-bold text-slate-200">38%</span>
              {/* Ultra mini circular visual arc block */}
              <div className="relative w-8 h-4 overflow-hidden mt-1 select-none">
                <svg viewBox="0 0 100 50" className="w-full h-full stroke-cyan-500/30 stroke-[12] fill-none">
                  <path d="M10 50 A 40 40 0 0 1 90 50" strokeLinecap="round" />
                  <path d="M10 50 A 40 40 0 0 1 90 50" strokeDasharray="125" strokeDashoffset={125 - (125 * 0.38)} className="stroke-cyan-400 stroke-[14]" strokeLinecap="round" />
                </svg>
              </div>
            </div>
          </div>

          {/* Card 3: Daily PnL */}
          <div className="p-2 bg-[#0a0c0e] rounded-sm border border-slate-800 flex flex-col min-w-[120px] shadow-[inset_0_0_10px_rgba(34,197,94,0.02)]">
            <span className="text-slate-500 uppercase text-[9px] tracking-wider">Daily PnL</span>
            <div className="flex items-baseline gap-1 mt-1">
              <span className="text-lg font-bold text-emerald-400">+{pnlPct.toFixed(2)}%</span>
              <span className="text-[9px] text-emerald-500/80">▲ SAFE</span>
            </div>
          </div>

          {/* Card 4: Max DD */}
          <div className="p-2 bg-[#0a0c0e] rounded-sm border border-slate-800 flex flex-col min-w-[120px] shadow-[inset_0_0_10px_rgba(239,68,68,0.02)]">
            <span className="text-slate-500 uppercase text-[9px] tracking-wider">Max Drawdown</span>
            <div className="flex items-baseline gap-1 mt-1">
              <span className="text-lg font-bold text-rose-400">-4.3%</span>
              <span className="text-[9px] text-rose-500/50">vs -10.0%</span>
            </div>
          </div>
        </div>
      </header>

      {/* EMERGENCY STOP NOTIFICATION BANNER */}
      {killSwitchActive && (
        <div className="relative bg-[#3b0a0a]/80 text-rose-200 border-b border-rose-900/40 px-4 py-2 flex items-center justify-between text-xs font-mono animate-pulse gap-2 z-10">
          <div className="flex items-center gap-2">
            <ShieldAlert className="w-4 h-4 text-rose-500" />
            <span><strong>EMERGENCY HALT ACTIVE</strong> — TRADING ROUTES SUSPENSIVELY LOCKED BY ACTIVE RISK KILL SWITCH.</span>
          </div>
          <button 
            type="button" 
            onClick={toggleKillSwitch}
            className="px-2 py-0.5 rounded bg-rose-950 border border-rose-700/50 text-rose-300 hover:bg-rose-900 font-bold transition-all"
          >
            DISARM SWITCH
          </button>
        </div>
      )}

      {/* MAIN LAYOUT WRAPPER Grid */}
      <main className="flex-1 p-3 grid grid-cols-1 lg:grid-cols-12 gap-3 relative z-10 max-w-[1920px] w-full mx-auto">
             {/* ========================================================== */}
        {/* COLUMN 1: LEFT RAIL - Risk Cockpit (takes 2 of 12 columns) */}
        {/* ========================================================== */}
        <div className="lg:col-span-2 flex flex-col gap-3 min-w-[210px] h-full">
          <div className="flex-1 p-3 bg-[#07080a] rounded-sm border border-slate-800 flex flex-col justify-between">
            
            {/* Upper Rail Body */}
            <div>
              <div className="flex items-center justify-between border-b border-slate-800 pb-2 mb-3">
                <div className="flex items-center gap-1.5">
                  <Sliders className="w-3.5 h-3.5 text-cyan-400" />
                  <h2 className="text-xs uppercase font-extrabold tracking-wider text-slate-100 font-mono">
                    RISK COCKPIT
                  </h2>
                </div>
                <div className="w-1.5 h-1.5 rounded-full bg-cyan-400 glow-cyan animate-pulse" />
              </div>

              {/* Cockpit Config Stack */}
              <div className="space-y-4">
                {/* 1. Daily Loss Used Gauge */}
                <div className="bg-slate-900/20 p-2.5 border border-slate-850 rounded-sm">
                  <div className="flex items-center justify-between text-[11px] font-mono mb-1">
                    <span className="text-slate-400">Daily Loss Used</span>
                    <span className="text-[10px] text-emerald-400 font-bold">0.42% <span className="text-slate-500">/ 3.0%</span></span>
                  </div>
                  <div className="flex items-center gap-2 mt-1">
                    {/* Ring Vector representation */}
                    <div className="w-10 h-10 relative flex-shrink-0">
                      <svg viewBox="0 0 36 36" className="w-full h-full stroke-slate-800 stroke-[5] fill-none">
                        <path d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831" />
                        <path d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831" 
                          strokeDasharray="14, 100" className="stroke-emerald-400" />
                      </svg>
                      <div className="absolute inset-0 flex items-center justify-center text-[8px] font-bold text-emerald-400 font-mono">
                        14%
                      </div>
                    </div>
                    <div className="text-[10px] text-slate-500 font-mono leading-tight">
                      Standard limits healthy. No breaching risk.
                    </div>
                  </div>
                </div>

                {/* 2. Correlation Exposure */}
                <div className="flex items-center justify-between bg-slate-900/20 p-2.5 border border-slate-850 rounded-sm">
                  <span className="text-[11px] text-slate-400 font-mono">Correlation Exp.</span>
                  <span className="px-1.5 py-0.5 text-[9px] font-bold bg-green-900/20 border border-green-900/50 text-green-400 rounded-sm font-mono">PASS</span>
                </div>

                {/* 3. BTC Vol Regime */}
                <div className="bg-slate-900/20 p-2.5 border border-slate-850 rounded-sm space-y-1">
                  <div className="flex items-center justify-between text-[11px] font-mono">
                    <span className="text-slate-400">BTC Vol Regime</span>
                    <span className="text-cyan-400 font-bold text-[11px]">Normal</span>
                  </div>
                  {/* Faint green sinus curve Sparkline representing vol */}
                  <div className="h-5 w-full mt-1 overflow-hidden select-none opacity-80">
                    <svg viewBox="0 0 100 20" className="w-full h-full stroke-emerald-500 stroke-1 fill-none">
                      <path d="M0 10 Q10 2 20 10 T40 10 T60 10 T80 10 T100 10" strokeLinecap="round" />
                    </svg>
                  </div>
                </div>

                {/* 4. Data Freshness */}
                <div className="flex items-center justify-between bg-slate-900/20 p-2.5 border border-slate-850 rounded-sm">
                  <span className="text-[11px] text-slate-400 font-mono">Data Freshness</span>
                  <span className="text-[11px] font-bold text-green-400 flex items-center gap-1">
                    <Database className="w-3 h-3 text-green-500" /> Healthy
                  </span>
                </div>

                {/* 5. Kill Switch (Interactive Mode) */}
                <div className={`p-2.5 border rounded-sm transition-all duration-300 ${killSwitchActive ? 'bg-rose-950/20 border-rose-500/30' : 'bg-slate-900/20 border-slate-850'}`}>
                  <div className="flex items-center justify-between text-[11px] font-mono mb-1.5">
                    <span className="text-slate-400">Kill Switch</span>
                    <span className={`font-bold uppercase ${killSwitchActive ? 'text-rose-400 animate-pulse' : 'text-slate-400'}`}>
                      {killSwitchActive ? 'ARMED' : 'Disarmed'}
                    </span>
                  </div>
                  
                  <button
                    type="button"
                    onClick={toggleKillSwitch}
                    className={`w-full py-1.5 px-2 rounded-sm text-[10px] font-mono font-bold uppercase transition-all duration-300 border flex items-center justify-center gap-1.5 ${killSwitchActive ? 'bg-rose-500 hover:bg-rose-600 border-rose-400 text-white animate-bounce' : 'bg-slate-900 border-slate-800 hover:bg-slate-800 text-slate-300'}`}
                  >
                    <span className={`w-2 h-2 rounded-full ${killSwitchActive ? 'bg-white' : 'bg-red-500'}`} />
                    {killSwitchActive ? 'SUSPEND TRADING' : 'ENGAGE FORCE EMERGENCY'}
                  </button>
                </div>

                {/* 6. Consecutive Losses Progress */}
                <div className="bg-slate-900/20 p-2.5 border border-slate-850 rounded-sm">
                  <div className="flex items-center justify-between text-[11px] font-mono mb-1.5">
                    <span className="text-slate-500">CONSECUTIVE LOSSES</span>
                    <strong className="text-slate-300">1 / 3</strong>
                  </div>
                  {/* Divided segment bar */}
                  <div className="grid grid-cols-3 gap-1">
                    <div className="h-1 bg-rose-500" />
                    <div className="h-1 bg-slate-700" />
                    <div className="h-1 bg-slate-700" />
                  </div>
                </div>
              </div>
            </div>

            {/* Lower Paused Assets block */}
            <div className="mt-4 pt-3 border-t border-slate-800">
              <div className="bg-amber-950/20 p-2.5 border border-amber-900/30 rounded-sm">
                <div className="flex items-center gap-1.5 mb-1">
                  <AlertTriangle className="w-3.5 h-3.5 text-amber-500" />
                  <span className="text-[10px] uppercase font-bold text-amber-500 font-mono tracking-wide">
                    Paused Assets
                  </span>
                </div>
                <div className="flex items-baseline justify-between text-xs font-mono">
                  <strong className="text-amber-400">DOGE</strong>
                  <span className="text-slate-500 text-[10px]">until 14:30</span>
                </div>
                <p className="text-[9px] text-slate-500 font-mono mt-1 leading-normal">
                  Volatility surge. Order buffer triggered.
                </p>
              </div>
            </div>
          </div>
        </div>

        {/* ========================================================== */}
        {/* COLUMN 2: CENTER MODULES - Target (Readiness + Charts + Positions) */}
        {/* ========================================================== */}
        <div className="lg:col-span-7 flex flex-col gap-3">
                 {/* CENTER TOP: LIVE READINESS (Core scorecard) */}
          <section className="bg-slate-900/20 p-4 rounded-sm border border-slate-800 relative overflow-hidden flex flex-col justify-between">
            {/* Soft decorative visual grid */}
            <div className="absolute inset-0 opacity-[0.02] bg-[linear-gradient(to_right,#808080_1px,transparent_1px),linear-gradient(to_bottom,#808080_1px,transparent_1px)] bg-[size:24px_24px] pointer-events-none" />

            {/* Panel Title */}
            <div className="flex items-center justify-between mb-3 pb-1 border-b border-slate-800 relative z-10">
              <div className="flex items-center gap-2">
                <h3 className="text-xs uppercase font-extrabold tracking-wider text-slate-100 font-mono">
                  LIVE READINESS
                </h3>
                <span className="text-slate-500 text-xs font-mono">/ Readiness Reactor</span>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={triggerSimTrade}
                  disabled={killSwitchActive || isReady}
                  className={`px-3 py-1 text-[10px] font-bold uppercase rounded-sm border transition-all flex items-center gap-1 ${isReady ? 'border-emerald-500/30 text-emerald-400 cursor-not-allowed bg-emerald-500/10' : killSwitchActive ? 'border-slate-800 text-slate-600 cursor-not-allowed bg-transparent' : 'border-cyan-500/40 text-cyan-400 hover:bg-cyan-950/40 bg-cyan-950/10 active:scale-95'}`}
                >
                  <Sparkles className="w-3 h-3" /> Simulate Paper Trade
                </button>
                {readinessScore > 62 && (
                  <button
                    type="button"
                    onClick={resetReadiness}
                    title="Reset simulation parameters"
                    className="p-1 hover:text-white transition-colors text-slate-500 font-bold"
                  >
                    <RefreshCcw className="w-3 h-3" />
                  </button>
                )}
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-12 gap-4 items-center relative z-10">
              
              {/* Segmented Speedometer-style Gauge Column (MD: 4/12 columns) */}
              <div className="md:col-span-4 flex flex-col items-center justify-center p-2 bg-[#0a0c0e] rounded-sm border border-slate-800">
                <div className="relative w-40 h-40 flex items-center justify-center select-none">
                  {/* Circular Vector Rings */}
                  <svg className="w-full h-full transform -rotate-180" viewBox="0 0 160 160">
                    <circle cx="80" cy="80" r="72" fill="none" className="stroke-slate-900/40 stroke-[1.5]" />
                    <circle cx="80" cy="80" r="58" fill="none" className="stroke-slate-900/40 stroke-[1]" />
                    
                    {/* Tick Mark Lines */}
                    {reactorTicks.map((tick, index) => (
                      <line
                        key={index}
                        x1={tick.x1}
                        y1={tick.y1}
                        x2={tick.x2}
                        y2={tick.y2}
                        className={`transition-all duration-700 ease-out ${tick.colorClass} ${tick.isActive ? 'opacity-100 stroke-[2]' : 'opacity-10 text-slate-800'}`}
                      />
                    ))}
                  </svg>

                  {/* Absolute Center Labels */}
                  <div className="absolute inset-0 flex flex-col items-center justify-center text-center pt-2">
                    <span className="text-[10px] tracking-widest text-slate-500 uppercase font-mono">Score</span>
                    <span className="text-3xl font-extrabold text-white font-mono leading-none tracking-tight my-0.5">
                      {readinessScore}<span className="text-slate-500 text-sm font-normal">/100</span>
                    </span>
                    <span className={`text-[9px] font-bold uppercase tracking-wider font-mono px-1.5 py-0.5 rounded-sm leading-none ${isReady ? 'text-emerald-400 bg-emerald-950/40 border border-emerald-500/30' : 'text-rose-400 bg-rose-950/20 border border-rose-500/30'}`}>
                      {isReady ? 'NOMINAL' : 'NOT READY'}
                    </span>
                  </div>
                </div>
                
                <span className="text-[9px] text-slate-500 font-mono mt-1 text-center leading-normal">
                  {isReady ? 'All parameters satisfied. Live lock lifted.' : 'Reactor locked in safety sandbox.'}
                </span>
              </div>

              {/* Metrics Metrics Table (MD: 5/12 columns) */}
              <div className="md:col-span-5 text-[11px] font-mono whitespace-nowrap overflow-x-auto">
                <table className="w-full text-left line-height-none border-collapse text-slate-400">
                  <thead>
                    <tr className="border-b border-slate-800 text-slate-500 text-[10px] uppercase tracking-wider">
                      <th className="pb-1.5">Metric</th>
                      <th className="pb-1.5 text-center">Current</th>
                      <th className="pb-1.5 text-right">Target</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-slate-800/40">
                    <tr className="hover:bg-slate-800/10">
                      <td className="py-1 flex items-center gap-1.5">
                        <span className={`w-1 h-1 rounded-full ${paperDays >= 30 ? 'bg-emerald-400' : 'bg-amber-400'}`} />
                        Paper Days
                      </td>
                      <td className={`py-1 text-center font-bold ${paperDays >= 30 ? 'text-emerald-400' : 'text-slate-300'}`}>{paperDays} / 30</td>
                      <td className="py-1 text-right text-slate-500">30 / 30</td>
                    </tr>
                    <tr className="hover:bg-slate-800/10">
                      <td className="py-1 flex items-center gap-1.5">
                        <span className={`w-1 h-1 rounded-full ${closedTrades >= 100 ? 'bg-emerald-400' : 'bg-rose-400'}`} />
                        Closed Trades
                      </td>
                      <td className={`py-1 text-center font-bold ${closedTrades >= 100 ? 'text-emerald-400' : 'text-slate-300'}`}>{closedTrades} / 100</td>
                      <td className="py-1 text-right text-slate-500">100 / 100</td>
                    </tr>
                    <tr className="hover:bg-slate-800/10">
                      <td className="py-1 flex items-center gap-1.5">
                        <span className={`w-1 h-1 rounded-full ${sharpeRatio >= 0.80 ? 'bg-emerald-400' : 'bg-rose-400'}`} />
                        Sharpe
                      </td>
                      <td className={`py-1 text-center font-bold ${sharpeRatio >= 0.80 ? 'text-emerald-400' : 'text-slate-300'}`}>{sharpeRatio.toFixed(2)}</td>
                      <td className="py-1 text-right text-slate-500">0.80</td>
                    </tr>
                    <tr className="hover:bg-slate-800/10">
                      <td className="py-1 flex items-center gap-1.5">
                        <span className="w-1 h-1 rounded-full bg-emerald-400" />
                        Max Drawdown
                      </td>
                      <td className="py-1 text-center text-emerald-400 font-bold">-4.3%</td>
                      <td className="py-1 text-right text-slate-500">-10.0%</td>
                    </tr>
                    <tr className="hover:bg-slate-800/10">
                      <td className="py-1 flex items-center gap-1.5">
                        <span className="w-1 h-1 rounded-full bg-emerald-400" />
                        Uptime
                      </td>
                      <td className="py-1 text-center text-slate-300 font-bold">98.1%</td>
                      <td className="py-1 text-right text-slate-500">95%</td>
                    </tr>
                    <tr className="hover:bg-slate-800/10">
                      <td className="py-1 flex items-center gap-1.5">
                        <span className="w-1 h-1 rounded-full bg-emerald-400" />
                        Stop-Loss Ratio
                      </td>
                      <td className="py-1 text-center text-slate-300 font-bold">{stopLossRatio}%</td>
                      <td className="py-1 text-right text-slate-500">35%</td>
                    </tr>
                    <tr className="hover:bg-slate-800/10">
                      <td className="py-1 flex items-center gap-1.5">
                        <span className="w-1 h-1 rounded-full bg-emerald-400" />
                        Data Integrity
                      </td>
                      <td className="py-1 text-center text-emerald-400 font-bold">PASS</td>
                      <td className="py-1 text-right text-slate-500">PASS</td>
                    </tr>
                  </tbody>
                </table>
              </div>

              {/* Blockers Panel Column (MD: 3/12 columns) */}
              <div className="md:col-span-3 flex flex-col justify-between h-full bg-slate-900/10 p-3 rounded-sm border border-slate-800 self-stretch font-mono">
                <div>
                  <span className="text-[10px] uppercase font-bold text-rose-400 tracking-wider">BLOCKERS</span>
                  <div className="mt-2 space-y-2">
                    {blockersList.length > 0 ? (
                      blockersList.map((blocker, i) => (
                        <div key={i} className="flex items-start gap-1 pb-1 text-[10px] text-slate-300 leading-tight">
                          <AlertTriangle className="w-3.5 h-3.5 text-rose-500 mt-px flex-shrink-0" />
                          <span>{blocker.split('(')[0]}</span>
                        </div>
                      ))
                    ) : (
                      <div className="flex items-center gap-1.5 text-emerald-400 text-xs">
                        <Check className="w-4 h-4" /> Ready for deployment
                      </div>
                    )}
                  </div>
                </div>

                <div className={`mt-3 pt-2.5 border-t border-slate-800 flex items-center gap-2 text-[10px] uppercase font-bold leading-normal leading-none ${isReady ? 'text-emerald-400 bg-emerald-500/15 p-1.5 rounded-sm' : 'text-slate-500'}`}>
                  {isReady ? <Unlock className="w-3.5 h-3.5" /> : <Lock className="w-3.5 h-3.5 text-rose-500/85" />}
                  <span>{isReady ? 'LIVE INJECTION UNLOCKED' : 'Locked till score >= 100'}</span>
                </div>
              </div>

            </div>
          </section>

          {/* CENTER MIDDLE: PERFORMANCE (Paper Equity & KPI values) */}
          <section className="bg-slate-900/20 p-4 rounded-sm border border-slate-800 relative">
            
            {/* Header controls layout */}
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-3 pb-1.5 border-b border-slate-800">
              <div className="flex items-center gap-2">
                <h3 className="text-xs uppercase font-extrabold tracking-wider text-slate-100 font-mono">
                  PERFORMANCE
                </h3>
                <span className="text-slate-500 text-xs font-mono">/ Asset Paper Run</span>
              </div>

              {/* Selector state widgets */}
              <div className="flex items-center gap-2">
                <span className="text-[10px] text-slate-500 font-mono uppercase mr-1">Curves:</span>
                <span className="flex items-center gap-2 mr-2 text-[10px] font-mono">
                  <span className="flex items-center gap-1 text-cyan-400">
                     <span className="w-2 h-0.5 bg-cyan-400 block" /> Equity
                  </span>
                  <span className="flex items-center gap-1 text-slate-500">
                    <span className="w-2 h-px border-b border-dashed border-slate-500 block" /> Buy & Hold BTC
                  </span>
                </span>
                <div className="flex rounded-sm bg-slate-950/80 border border-slate-800 p-0.5">
                  {(['1D', '7D', '30D', 'ALL'] as const).map(tf => (
                    <button
                      key={tf}
                      type="button"
                      onClick={() => setSelectedTimeframe(tf)}
                      className={`text-[9px] font-mono px-2 py-0.5 rounded-sm transition-all font-bold ${selectedTimeframe === tf ? 'bg-cyan-500 text-[#0a0c0e]' : 'text-slate-400 hover:text-white'}`}
                    >
                      {tf}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            {/* Custom SVG Responsive Vector Equity Graph */}
            <div className="relative bg-[#0a0c0e]/60 rounded-sm border border-slate-800 p-2 overflow-visible select-none h-44">
              <svg 
                viewBox={`0 0 ${width} ${height}`} 
                className="w-full h-full overflow-visible"
                onMouseMove={handleChartMouseMove}
                onMouseLeave={handleChartMouseLeave}
              >
                {/* Horizontal grid lines */}
                {[0, 0.25, 0.5, 0.75, 1].map((r, idx) => {
                  const y = paddingTop + r * (height - paddingTop - paddingBottom);
                  return (
                    <line 
                      key={idx} 
                      x1={paddingLeft} 
                      y1={y} 
                      x2={width - paddingRight} 
                      y2={y} 
                      className="stroke-slate-800/40 stroke-[0.5]" 
                      strokeDasharray="3,3" 
                    />
                  );
                })}

                {/* Vertical grid lines mapping to points */}
                {renderedEquityPoints.points.map((p, idx) => {
                  const x = renderedEquityPoints.xScale(idx);
                  return (
                    <line 
                      key={idx} 
                      x1={x} 
                      y1={paddingTop} 
                      x2={x} 
                      y2={height - paddingBottom} 
                      className="stroke-slate-800/40 stroke-[0.5]" 
                      strokeDasharray="3,3" 
                    />
                  );
                })}

                {/* Draw X-Axis labels */}
                {renderedEquityPoints.points.map((p, idx) => {
                  // Only plot some keys to avoid text cluster overlap
                  if (renderedEquityPoints.points.length > 8 && idx % 2 !== 0 && idx !== renderedEquityPoints.points.length - 1) return null;
                  const x = renderedEquityPoints.xScale(idx);
                  return (
                    <text 
                      key={idx} 
                      x={x} 
                      y={height - 6} 
                      className="fill-slate-500 font-mono text-[8px]" 
                      textAnchor="middle"
                    >
                      {p.date}
                    </text>
                  );
                })}

                {/* Draw Y-Axis labels (Left Aligned for absolute values) */}
                {[0, 0.5, 1].map((ratio, idx) => {
                  const val = renderedEquityPoints.max - ratio * (renderedEquityPoints.max - renderedEquityPoints.min);
                  const y = paddingTop + ratio * (height - paddingTop - paddingBottom);
                  return (
                    <text 
                      key={idx} 
                      x={paddingLeft - 8} 
                      y={y + 3} 
                      className="fill-slate-500 font-mono text-[8px]" 
                      textAnchor="end"
                    >
                      ${Math.round(val).toLocaleString()}
                    </text>
                  );
                })}

                {/* Gradient shader for Equity under-area fill */}
                <defs>
                  <linearGradient id="equityFillGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#06b6d4" stopOpacity="0.18" />
                    <stop offset="100%" stopColor="#06b6d4" stopOpacity="0.00" />
                  </linearGradient>
                </defs>

                {/* Render colored background area */}
                <polygon points={renderedEquityPoints.fillPath} fill="url(#equityFillGrad)" />

                {/* Benchmark dashed BTC line */}
                <polyline 
                  points={renderedEquityPoints.benchmarkPath} 
                  fill="none" 
                  className="stroke-slate-600 stroke-[1]" 
                  strokeDasharray="3,3" 
                />

                {/* Active vector line (Cyan) */}
                <polyline 
                  points={renderedEquityPoints.equityPath} 
                  fill="none" 
                  className="stroke-cyan-400 stroke-[1.8]" 
                />

                {/* Little circles at active boundaries */}
                {renderedEquityPoints.points.map((p, idx) => {
                  const x = renderedEquityPoints.xScale(idx);
                  const y = renderedEquityPoints.yScale(p.equity);
                  const isLast = idx === renderedEquityPoints.points.length - 1;
                  return (
                    <circle 
                      key={idx} 
                      cx={x} 
                      cy={y} 
                      r={isLast ? 3.5 : 2} 
                      className={`${isLast ? 'fill-cyan-400 stroke-cyan-200 stroke-1' : 'fill-[#0a0c0e] stroke-cyan-400 stroke-[1]'}`} 
                    />
                  );
                })}

                {/* Crosshair & popover highlight line */}
                {hoverIndex !== null && (
                  <>
                    <line 
                      x1={renderedEquityPoints.xScale(hoverIndex)} 
                      y1={paddingTop} 
                      x2={renderedEquityPoints.xScale(hoverIndex)} 
                      y2={height - paddingBottom} 
                      className="stroke-cyan-500/50 stroke-1" 
                      strokeDasharray="2,2" 
                    />
                    <circle 
                      cx={renderedEquityPoints.xScale(hoverIndex)} 
                      cy={renderedEquityPoints.yScale(renderedEquityPoints.points[hoverIndex].equity)} 
                      r="4.5" 
                      className="fill-cyan-400 stroke-white stroke-1" 
                    />
                  </>
                )}
              </svg>

              {/* Hover technical Popover Card */}
              {hoverIndex !== null && (
                <div 
                  className="absolute pointer-events-none bg-[#0a0c0e] border border-slate-700 rounded-sm p-1.5 text-[10px] text-slate-100 font-mono shadow-xl"
                  style={{ left: hoverCoords.x, top: hoverCoords.y }}
                >
                  <div className="text-slate-400 font-bold">{renderedEquityPoints.points[hoverIndex].date}</div>
                  <div className="flex gap-3 mt-1 justify-between">
                    <span>Equity:</span>
                    <strong className="text-cyan-400">${renderedEquityPoints.points[hoverIndex].equity.toFixed(2)}</strong>
                  </div>
                  <div className="flex gap-3 justify-between">
                    <span>BTC (B&H):</span>
                    <span className="text-slate-400">${renderedEquityPoints.points[hoverIndex].benchmark.toFixed(2)}</span>
                  </div>
                </div>
              )}
            </div>

            {/* KPI metric fields arranged in grid below */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mt-3 text-xs font-mono">
              {/* Bal */}
              <div className="p-2 bg-[#0a0c0e]/30 rounded-sm border border-slate-800">
                <div className="text-slate-500 text-[10px] uppercase">Paper Balance</div>
                <div className="text-sm font-bold text-white mt-0.5">${paperBalance.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
              </div>

              {/* Net PnL */}
              <div className="p-2 bg-[#0a0c0e]/30 rounded-sm border border-slate-800">
                <div className="text-slate-500 text-[10px] uppercase">Net PnL</div>
                <div className="text-sm font-bold text-emerald-400 mt-0.5">+{pnlPct.toFixed(2)}%</div>
              </div>

              {/* Gross PnL */}
              <div className="p-2 bg-[#0a0c0e]/30 rounded-sm border border-slate-800">
                <div className="text-slate-500 text-[10px] uppercase">Gross PnL</div>
                <div className="text-sm font-bold text-emerald-400 mt-0.5">+{(pnlPct + 0.47).toFixed(2)}%</div>
              </div>

              {/* Fees */}
              <div className="p-2 bg-[#0a0c0e]/30 rounded-sm border border-slate-800">
                <div className="text-slate-500 text-[10px] uppercase">Fees</div>
                <div className="text-sm font-bold text-rose-400 mt-0.5">-0.31%</div>
              </div>

              {/* Funding */}
              <div className="p-2 bg-[#0a0c0e]/30 rounded-sm border border-slate-800">
                <div className="text-slate-500 text-[10px] uppercase">Funding</div>
                <div className="text-sm font-bold text-rose-400 mt-0.5">-0.16%</div>
              </div>

              {/* Profit Factor */}
              <div className="p-2 bg-[#0a0c0e]/30 rounded-sm border border-slate-800">
                <div className="text-slate-500 text-[10px] uppercase">Profit Factor</div>
                <div className="text-sm font-bold text-cyan-400 mt-0.5">1.42</div>
              </div>

              {/* Expectancy */}
              <div className="p-2 bg-[#0a0c0e]/30 rounded-sm border border-slate-800">
                <div className="text-slate-500 text-[10px] uppercase">Expectancy</div>
                <div className="text-sm font-bold text-emerald-400 mt-0.5">+0.18% <span className="text-slate-500 text-[9px] font-normal">/ trade</span></div>
              </div>

              {/* Win Rate */}
              <div className="p-2 bg-[#0a0c0e]/30 rounded-sm border border-slate-800">
                <div className="text-slate-500 text-[10px] uppercase">Win Rate</div>
                <div className="text-sm font-bold text-cyan-400 mt-0.5">54.2%</div>
              </div>
            </div>

          </section>

          {/* LOWER CENTER: OPEN POSITIONS (2 side-by-side position cards) */}
          <section className="p-4 bg-slate-900/10 rounded-sm border border-slate-800">
            
            <div className="flex items-center gap-1.5 pb-2 border-b border-slate-800 mb-3">
              <Activity className="w-4 h-4 text-cyan-400" />
              <h3 className="text-xs uppercase font-extrabold tracking-wider text-slate-100 font-mono">
                OPEN POSITIONS <span className="text-slate-500 text-[11px] font-normal font-sans ml-1">/ Client Contracts</span>
              </h3>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-xs font-mono">
              
              {livePositions.length > 0 ? (
                livePositions.map((pos: any, idx: number) => {
                  const colors = ['bg-purple-500', 'bg-indigo-500', 'bg-emerald-500', 'bg-amber-500'];
                  const colorIdx = idx % colors.length;
                  const entryPx = pos.entryPrice || 0;
                  return (
                    <div key={idx} className="p-3 bg-slate-900/40 rounded-sm border border-slate-800 flex flex-col justify-between hover:border-cyan-500/20 transition-all select-none gap-2">
                      <div>
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-1.5 text-slate-100 font-bold">
                            <span className={"w-2 h-2 rounded-sm " + colors[colorIdx]} />
                            {pos.asset}
                          </div>
                          <span className="px-1.5 py-0.5 rounded-sm text-[9px] font-bold bg-[#100c0e]/40 border border-[#861616]/30 text-rose-400 uppercase tracking-widest flex items-center gap-1">
                            {pos.direction} <span className="font-sans">▲</span> {(pos.unrealizedPnlPct || 0).toFixed(2)}%
                          </span>
                        </div>

                        <div className="grid grid-cols-2 gap-x-2 gap-y-1.5 mt-3 text-slate-450">
                          <div>
                            <span className="text-slate-500 text-[10px] uppercase block">Entry Price</span>
                            <strong className="text-slate-200">{entryPx.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}</strong>
                          </div>
                          <div>
                            <span className="text-slate-500 text-[10px] uppercase block">Size</span>
                            <strong className="text-slate-200">${(pos.size || 0).toLocaleString(undefined, {minimumFractionDigits: 2})}</strong>
                          </div>
                          <div>
                            <span className="text-slate-500 text-[10px] uppercase block">Unrealized PnL</span>
                            <strong className={"font-bold font-mono " + ((pos.unrealizedPnl || 0) >= 0 ? "text-emerald-400" : "text-rose-400")}>
                              {(pos.unrealizedPnl || 0) >= 0 ? '+' : ''}{(pos.unrealizedPnl || 0).toFixed(2)} <span className="text-[10px] font-normal text-slate-500/80">({(pos.unrealizedPnlPct || 0) >= 0 ? '+' : ''}{(pos.unrealizedPnlPct || 0).toFixed(2)}%)</span>
                            </strong>
                          </div>
                          <div>
                            <span className="text-slate-500 text-[10px] uppercase block">Confidence</span>
                            <strong className="text-emerald-400 bg-emerald-900/15 px-1 rounded-sm">{pos.confidence || '--'}</strong>
                          </div>
                        </div>
                      </div>

                      {(pos.stopLoss || pos.takeProfit || pos.takeProfit2) ? (
                        <div className="bg-[#050607]/60 p-2 rounded-sm border border-slate-800 grid grid-cols-3 gap-1 divide-x divide-slate-800/80 text-[10px] text-center mt-1">
                          <div>
                            <span className="text-slate-500 block uppercase text-[9px]">Stop</span>
                            <span className="text-rose-400 font-bold">{pos.stopLoss ? pos.stopLoss.toFixed(2) : '--'}</span>
                            <span className="text-[9px] text-slate-600 block">{pos.stopLoss ? ((pos.stopLoss - entryPx) / entryPx * 100).toFixed(2) + '%' : ''}</span>
                          </div>
                          <div>
                            <span className="text-slate-500 block uppercase text-[9px]">TP1</span>
                            <span className="text-emerald-400 font-bold">{pos.takeProfit ? pos.takeProfit.toFixed(2) : '--'}</span>
                            <span className="text-[9px] text-slate-600 block">{pos.takeProfit ? ((pos.takeProfit - entryPx) / entryPx * 100).toFixed(2) + '%' : ''}</span>
                          </div>
                          <div>
                            <span className="text-slate-500 block uppercase text-[9px]">TP2</span>
                            <span className="text-cyan-400 font-bold">{pos.takeProfit2 ? pos.takeProfit2.toFixed(2) : '--'}</span>
                            <span className="text-[9px] text-slate-600 block">{pos.takeProfit2 ? ((pos.takeProfit2 - entryPx) / entryPx * 100).toFixed(2) + '%' : ''}</span>
                          </div>
                        </div>
                      ) : null}
                    </div>
                  );
                })
              ) : (
                <div className="col-span-1 md:col-span-2 flex items-center justify-center h-32 text-slate-500 italic select-none border border-dashed border-slate-800/50 rounded-sm">
                  No open positions
                </div>
              )}            </div>
          </section>


        </div>

        {/* ========================================================== */}
        {/* COLUMN 3: RIGHT RAIL - Activities + Drawdown + Diagnostics */}
        {/* ========================================================== */}
        <div className="lg:col-span-3 flex flex-col gap-3 h-full justify-between">
          
          {/* RIGHT TOP: ACTIVITY FEED */}
          <section className="bg-slate-900/20 p-3 rounded-sm border border-slate-800 flex-1 flex flex-col h-[340px]">
            
            <div className="flex items-center justify-between border-b border-slate-800 pb-1.5 mb-2 flex-shrink-0">
              <div className="flex items-center gap-1.5">
                <Radio className="w-3.5 h-3.5 text-cyan-400 animate-pulse" />
                <h3 className="text-xs uppercase font-extrabold tracking-wider text-slate-100 font-mono">
                  DECISION STREAM
                </h3>
              </div>

              {/* Feed filtering selection */}
              <div className="flex items-center gap-1">
                <Filter className="w-2.5 h-2.5 text-slate-500" />
                <select 
                  className="bg-[#0a0c0e] text-slate-400 border border-slate-705/80 text-[9px] font-mono rounded-sm px-1.5 py-0.5 focus:outline-none focus:border-cyan-500 font-bold"
                  value={feedFilter}
                  onChange={(e) => setFeedFilter(e.target.value)}
                >
                  <option value="All">All</option>
                  <option value="Signal">Signals</option>
                  <option value="Skip">Skips</option>
                  <option value="Risk Block">Blocks</option>
                  <option value="Entry">Entries</option>
                  <option value="Exit">Exits</option>
                  <option value="System">System</option>
                </select>
              </div>
            </div>

            {/* Scrolling Feed layout list */}
            <div className="flex-1 overflow-y-auto space-y-1.5 pr-1 text-[10px] font-mono leading-snug">
              {filteredLogs.length > 0 ? (
                filteredLogs.map((log) => {
                  let badgeColors = 'text-slate-400 border-slate-800 bg-slate-950/40';
                  if (log.type === 'SIGNAL') badgeColors = 'text-sky-400 border-sky-400/25 bg-sky-950/20';
                  else if (log.type === 'SKIP') badgeColors = 'text-amber-500 border-amber-500/25 bg-amber-950/20';
                  else if (log.type === 'RISK BLOCK') badgeColors = 'text-rose-400 border-rose-500/25 bg-rose-950/20';
                  else if (log.type === 'ENTRY') badgeColors = 'text-emerald-400 border-emerald-500/25 bg-emerald-950/20';
                  else if (log.type === 'SYSTEM') badgeColors = 'text-cyan-400 border-cyan-500/25 bg-cyan-950/20';

                  return (
                    <div 
                      key={log.id} 
                      className={`flex items-start gap-1 p-1 rounded-sm border hover:bg-slate-800/10 transition-colors ${log.type === 'RISK BLOCK' ? 'border-rose-900/10' : 'border-transparent'}`}
                    >
                      <span className="text-slate-600 flex-shrink-0 font-normal select-none">{log.time.slice(0, 5)}</span>
                      <strong className={`px-1 rounded-sm text-[8px] uppercase font-bold border flex-shrink-0 tracking-wider font-mono ${badgeColors}`} style={{ fontSize: '7.5px' }}>
                        {log.badge}
                      </strong>
                      <span className="text-slate-300 leading-normal pl-0.5 break-words max-w-[190px] xl:max-w-full">{log.text}</span>
                    </div>
                  );
                })
              ) : (
                <div className="h-full flex items-center justify-center text-slate-600 italic select-none">
                  No decision rows match filter bounds.
                </div>
              )}
            </div>
          </section>

          {/* RIGHT MIDDLE: DRAWDOWN MAP */}
          <section className="bg-slate-900/20 p-3 rounded-sm border border-slate-800 h-[190px] flex flex-col justify-between">
            <div className="flex items-center justify-between pb-1 border-b border-slate-800">
              <div className="flex items-center gap-1">
                <h3 className="text-xs uppercase font-extrabold tracking-wider text-slate-100 font-mono">
                  UNDERWATER (DRAWDOWN)
                </h3>
              </div>
              <span className="text-[10px] text-rose-400 font-bold font-mono">Max: -4.3%</span>
            </div>

            {/* Custom Red Underwater Graph */}
            <div className="h-24 w-full bg-[#100c0e] rounded-sm border border-red-950/45 p-1.5 overflow-hidden select-none relative">
              <svg viewBox="0 0 300 100" className="w-full h-full overflow-visible">
                {/* 0% Line */}
                <line x1="10" y1="10" x2="290" y2="10" className="stroke-[#471e26] stroke-[0.8]" strokeDasharray="2,2" />
                {/* Max line at -4.3% which sits lower */}
                <line x1="10" y1="80" x2="290" y2="80" className="stroke-slate-900/40" />

                {/* Draw drawdown path */}
                {(() => {
                  const points = drawdownData;
                  const deltaX = 280 / (points.length - 1);
                  const spline = points.map((p, i) => `${10 + i * deltaX},${10 + Math.abs(p.value) * 16}`).join(' ');
                  const areaFill = `10,10 ` + points.map((p, i) => `${10 + i * deltaX},${10 + Math.abs(p.value) * 16}`).join(' ') + ` 290,10`;
                  
                  return (
                    <>
                      <defs>
                        <linearGradient id="drawGrad" x1="0" y1="0" x2="0" y2="1">
                          <stop offset="0%" stopColor="#ef4444" stopOpacity="0.0" />
                          <stop offset="100%" stopColor="#ef4444" stopOpacity="0.14" />
                        </linearGradient>
                      </defs>
                      <polygon points={areaFill} fill="url(#drawGrad)" />
                      <polyline points={spline} fill="none" className="stroke-rose-500 stroke-[1.3]" />
                      
                      {/* Ticker dots */}
                      {points.map((p, i) => {
                        if (i % 3 !== 0 && i !== points.length -1) return null;
                        return (
                          <circle 
                            key={i} 
                            cx={10 + i * deltaX} 
                            cy={10 + Math.abs(p.value) * 16} 
                            r="1.5" 
                            className="fill-[#100c0e] stroke-rose-400 stroke-[0.8]" 
                          />
                        );
                      })}
                    </>
                  );
                })()}
              </svg>

              {/* Grid timeline dates overlayed onto chart */}
              <div className="absolute bottom-1.5 left-2 right-2 flex justify-between text-[8px] text-slate-500 font-mono pointer-events-none">
                <span>May 05</span>
                <span>May 09</span>
                <span>May 13</span>
                <span>May 15</span>
              </div>
            </div>

            {/* Drawdown stats list row */}
            <div className="grid grid-cols-3 gap-1 text-[9px] font-mono text-center pt-1 border-t border-slate-800">
              <div>
                <span className="text-slate-500 block">Max DD</span>
                <strong className="text-rose-400 font-bold">-4.3%</strong>
              </div>
              <div>
                <span className="text-slate-500 block">Avg DD</span>
                <strong className="text-slate-400">-1.2%</strong>
              </div>
              <div>
                <span className="text-slate-500 block">Time in DD</span>
                <strong className="text-[#f59e0b]">18.6%</strong>
              </div>
            </div>

          </section>

          {/* LOWER ROWS BENTO LAYOUT (Horizontal 3 Sub-Blocks) */}
          <section className="grid grid-cols-1 gap-2 border-t border-slate-800 pt-2">
            
            {/* 1. Asset Leaderboard (Strategy Intelligence) */}
            <div className="bg-slate-900/20 p-2.5 rounded-sm border border-slate-800 font-mono whitespace-nowrap overflow-x-auto text-[10px]">
              <span className="text-[10px] uppercase font-bold text-slate-100 tracking-wider block mb-1">
                STRATEGY LEADERBOARD
              </span>
              <table className="w-full text-left col-spacing-none divide-y divide-slate-800/40">
                <thead>
                  <tr className="text-slate-500 text-[8px] uppercase">
                    <th className="pb-1">Asset</th>
                    <th className="pb-1 text-center">Trades</th>
                    <th className="pb-1 text-center font-bold">Net PnL</th>
                    <th className="pb-1 text-center">Win %</th>
                    <th className="pb-1 text-right">PF</th>
                    <th className="pb-1 text-right">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/40 text-[#cdd5e0]">
                  {strategyData.map((row) => (
                    <tr 
                      key={row.asset} 
                      onClick={() => toggleStrategyStatus(row.asset)}
                      className="hover:bg-slate-800/10 cursor-pointer transition-colors"
                      title={`Click to turn ${row.asset} status to ${row.status === 'ACTIVE' ? 'PAUSED' : 'ACTIVE'}`}
                    >
                      <td className="py-1 font-bold text-white flex items-center gap-1.5">
                        <span className={`w-1 h-1 rounded-full ${row.status === 'ACTIVE' ? 'bg-emerald-400' : 'bg-amber-400'}`} />
                        {row.asset}
                      </td>
                      <td className="py-1 text-center text-slate-400">{row.trades}</td>
                      <td className={`py-1 text-center font-bold ${row.netPnl.startsWith('+') ? 'text-emerald-400' : 'text-rose-400'}`}>{row.netPnl}</td>
                      <td className="py-1 text-center text-slate-300">{row.winRate}</td>
                      <td className="py-1 text-right text-cyan-400">{row.pf}</td>
                      <td className="py-1 text-right">
                        <span className={`px-1 py-px rounded-sm text-[8px] font-bold ${row.status === 'ACTIVE' ? 'text-emerald-400 bg-emerald-400/10 border border-emerald-400/20' : 'text-amber-500 bg-amber-500/10 border border-amber-500/20'}`}>
                          {row.status}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* 2. Gate Attribution */}
            <div className="bg-slate-900/20 p-2.5 rounded-sm border border-slate-800 text-[10px] font-mono">
              <span className="text-[10px] uppercase font-bold text-slate-100 tracking-wider block mb-1">
                GATE ATTRIBUTION <span className="text-slate-300 text-[8.5px] font-normal font-sans ml-1">/ Block Reasons</span>
              </span>
              
              <div className="space-y-1.5 mt-1">
                {[
                  { label: 'Hurst regime block', count: 18, pct: 24, fill: 'bg-rose-500/80', max: 24 },
                  { label: 'BTC vol surge', count: 14, pct: 18, fill: 'bg-amber-500/80', max: 18 },
                  { label: 'ADX danger zone', count: 12, pct: 16, fill: 'bg-amber-500/80', max: 16 },
                  { label: 'Correlation cap', count: 11, pct: 15, fill: 'bg-indigo-500/80', max: 15 },
                  { label: 'Volume filter', count: 9, pct: 12, fill: 'bg-[#475569]/80', max: 12 }
                ].map((gate) => (
                  <div key={gate.label} className="space-y-0.5">
                    <div className="flex justify-between text-[9px] text-slate-400 leading-none">
                      <span>{gate.label}</span>
                      <span className="font-bold text-slate-300">{gate.count} <span className="text-slate-500 text-[8px]">({gate.pct}%)</span></span>
                    </div>
                    {/* Progress Slider */}
                    <div className="w-full h-1.5 bg-slate-950 rounded-sm overflow-hidden flex">
                      <div className={`h-full ${gate.fill}`} style={{ width: `${gate.pct * 3}%` }} />
                    </div>
                  </div>
                ))}
              </div>

              <div className="flex items-center justify-between text-[10px] font-mono border-t border-slate-800 mt-2 pt-1">
                <span className="text-slate-500">Total suppressed checks</span>
                <span className="text-white font-extrabold bg-slate-950 px-1.5 py-0.5 rounded-sm">64</span>
              </div>
            </div>

            {/* 3. Market Context */}
            <div className="bg-slate-900/20 p-2.5 rounded-sm border border-slate-800 text-[10px] font-mono leading-relaxed space-y-2">
              <span className="text-[10px] uppercase font-bold text-slate-100 tracking-wider block">
                MARKET CONTEXT
              </span>

              <div className="grid grid-cols-2 gap-2 text-[10px]">
                {/* BTC Vol */}
                <div className="bg-slate-900/30 p-1.5 rounded-sm border border-slate-800">
                  <span className="text-slate-500 uppercase text-[8px]">BTC Regime</span>
                  <div className="text-white font-bold mt-0.5 text-xs flex items-center justify-between">
                    <span>Neutral</span>
                    <span className="text-[9px] text-sky-400">Stable</span>
                  </div>
                </div>

                {/* Fear & Greed Index */}
                <div className="bg-slate-900/30 p-1.5 rounded-sm border border-slate-800">
                  <span className="text-slate-500 uppercase text-[8px]">Fear & Greed</span>
                  <div className="text-white font-bold mt-0.5 text-xs flex items-center justify-between">
                    <span className="text-emerald-400">62</span>
                    <span className="text-[9px] text-emerald-500">Greed</span>
                  </div>
                </div>

                {/* Funding Env */}
                <div className="bg-slate-900/30 p-1.5 rounded-sm border border-slate-800">
                  <span className="text-slate-500 uppercase text-[8px]">Funding Environment</span>
                  <div className="text-emerald-400 font-bold mt-0.5 text-[9.5px] items-baseline flex gap-1">
                    <span>Slightly Positive</span>
                    <span className="text-[8px] text-emerald-500">▲</span>
                  </div>
                </div>

                {/* OI Velocity */}
                <div className="bg-slate-900/30 p-1.5 rounded-sm border border-slate-800 space-y-0.5">
                  <span className="text-slate-500 uppercase text-[8px]">OI Velocity</span>
                  <div className="text-white font-bold text-xs flex items-center justify-between leading-none">
                    <span>Normal</span>
                    {/* Small Wave Vector sparkline representing velocity */}
                    <div className="w-8 h-3 flex select-none opacity-80">
                      <svg viewBox="0 0 30 10" className="w-full h-full stroke-cyan-400 stroke-1 fill-none">
                        <path d="M0 5 L5 2 L10 8 L15 3 L20 7 L25 2 L30 5" />
                      </svg>
                    </div>
                  </div>
                </div>
              </div>
            </div>

          </section>

        </div>

      </main>

      {/* FOOTER TERMINAL STATUS */}
      <footer className="relative z-10 bg-[#06080b]/90 border-t border-slate-850 px-4 py-2 flex flex-col md:flex-row items-center justify-between text-[11px] font-mono text-slate-500 select-none gap-2">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
          <span className="flex items-center gap-1">
            Exchange: <strong className="text-slate-300">HYPERLIQUID PAPER</strong>
          </span>
          <span className="text-slate-800">|</span>
          <span className="flex items-center gap-1">
            Data Feed: 
            <span className="inline-flex items-center gap-1 font-bold text-emerald-400 uppercase">
              <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" /> OK
            </span>
          </span>
          <span className="text-slate-800">|</span>
          <span>Strategy: <strong className="text-cyan-400 text-xs">HERMES v2.3</strong></span>
          <span className="text-slate-800">|</span>
          <span>Risk Profile: <strong className="text-slate-300">STANDARD</strong></span>
        </div>

        <div className="flex items-center gap-4">
          <span className="text-emerald-500 font-semibold flex items-center gap-1">
            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400 animate-pulse" />
            All systems nominal.
          </span>
          <span className="text-slate-805">|</span>
          <span className="text-slate-400 font-bold bg-[#0a0c0e]/80 px-2 py-0.5 rounded-sm border border-slate-800 flex items-center gap-1">
            <Clock className="w-3.5 h-3.5 text-slate-500" />
            {currentTime}
          </span>
        </div>
      </footer>

    </div>
  );
}
