import { useEffect, useRef, useState } from 'react'
import { Plus, Trash2, Edit2, RotateCw } from 'lucide-react'
import api from '../api/client'
import { apiErrorMessage } from '../api/errors'
import { parseAddressPort, formatAddressPort } from '../utils/addressUtils'
import { useLanguage } from '../contexts/LanguageContext'

interface Tunnel {
  id: string
  name: string
  core: string
  type: string
  node_id: string
  iran_node_id?: string
  foreign_node_id?: string
  spec: Record<string, any>
  status: string
  error_message?: string | null
  revision: number
  created_at: string
  updated_at: string
}

interface PortLive {
  port: number
  status: 'live' | 'offline' | 'error' | string
  error?: string | null
}

interface TunnelLive {
  status: 'live' | 'offline' | 'error' | string
  process_running?: boolean
  ports: PortLive[]
  control?: PortLive | null
  error?: string | null
}

type BackhaulTransport = 'tcp' | 'udp' | 'ws' | 'wsmux' | 'tcpmux'

interface BackhaulFormState {
  transport: BackhaulTransport
  control_port: string
  public_port: string
  listen_ip: string
  public_host: string
  remote_addr: string
  target_host: string
  target_port: string
  token: string
  accept_udp: boolean
}

interface BackhaulAdvancedServerState {
  keepalive_period: string
  heartbeat: string
  channel_size: string
  mux_con: string
  log_level: string
  nodelay: boolean
  skip_optz: boolean
  tls_cert: string
  tls_key: string
  sniffer: boolean
  web_port: string
  proxy_protocol: boolean
}

interface BackhaulAdvancedClientState {
  connection_pool: string
  retry_interval: string
  dial_timeout: string
  keepalive_period: string
  log_level: string
  nodelay: boolean
  aggressive_pool: boolean
  edge_ip: string
  skip_optz: boolean
}

interface BackhaulAdvancedState {
  server: BackhaulAdvancedServerState
  client: BackhaulAdvancedClientState
  customPorts: string
}

const createDefaultBackhaulState = (): BackhaulFormState => ({
  transport: 'tcp',
  control_port: '3080',
  public_port: '443',
  listen_ip: '0.0.0.0',
  public_host: '',
  remote_addr: '',
  target_host: '127.0.0.1',
  target_port: '8080',
  token: '',
  accept_udp: false,
})

const createDefaultBackhaulAdvancedState = (): BackhaulAdvancedState => ({
  server: {
    keepalive_period: '75',
    heartbeat: '40',
    channel_size: '2048',
    mux_con: '8',
    log_level: 'info',
    nodelay: true,
    skip_optz: false,
    tls_cert: '',
    tls_key: '',
    sniffer: false,
    web_port: '',
    proxy_protocol: false,
  },
  client: {
    connection_pool: '4',
    retry_interval: '3',
    dial_timeout: '10',
    keepalive_period: '75',
    log_level: 'info',
    nodelay: true,
    aggressive_pool: false,
    edge_ip: '',
    skip_optz: false,
  },
  customPorts: '',
})

const numericServerKeys = new Set([
  'keepalive_period',
  'heartbeat',
  'channel_size',
  'mux_con',
  'web_port',
])
const booleanServerKeys = new Set(['nodelay', 'skip_optz', 'sniffer', 'proxy_protocol'])
const stringServerKeys = new Set(['log_level', 'tls_cert', 'tls_key', 'sniffer_log'])

const numericClientKeys = new Set(['connection_pool', 'retry_interval', 'dial_timeout', 'keepalive_period'])
const booleanClientKeys = new Set(['nodelay', 'aggressive_pool', 'skip_optz'])
const stringClientKeys = new Set(['log_level', 'edge_ip'])

interface BackhaulDisplayInfo {
  controlPort: string
  publicPort: string
  target: string
}

const getBackhaulDisplayInfo = (spec: Record<string, any> | undefined): BackhaulDisplayInfo => {
  if (!spec) {
    return { controlPort: 'N/A', publicPort: 'N/A', target: 'N/A' }
  }

  const controlPort =
    spec.control_port ||
    (typeof spec.bind_addr === 'string' && spec.bind_addr.includes(':') ? spec.bind_addr.split(':').pop() : undefined) ||
    (typeof spec.remote_addr === 'string' && spec.remote_addr.includes(':') ? spec.remote_addr.split(':').pop() : undefined) ||
    'N/A'

  const publicPort =
    spec.public_port ||
    spec.listen_port ||
    (Array.isArray(spec.ports) && spec.ports.length > 0
      ? (() => {
          const [first] = spec.ports
          if (typeof first !== 'string') return undefined
          const [left] = first.split('=')
          const parts = left.split(':')
          return parts.pop()
        })()
      : undefined) ||
    'N/A'

  const target =
    spec.target_addr ||
    (Array.isArray(spec.ports) && spec.ports.length > 0
      ? (() => {
          const [first] = spec.ports
          if (typeof first !== 'string') return undefined
          const segments = first.split('=')
          return segments.length > 1 ? segments[1] : undefined
        })()
      : undefined) ||
    'N/A'

  return {
    controlPort: controlPort?.toString() || 'N/A',
    publicPort: publicPort?.toString() || 'N/A',
    target: target?.toString() || 'N/A',
  }
}

const Tunnels = () => {
  const { t } = useLanguage()
  const [tunnels, setTunnels] = useState<Tunnel[]>([])
  const [nodes, setNodes] = useState<any[]>([])
  const [servers, setServers] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [showAddModal, setShowAddModal] = useState(false)
  const [editingTunnel, setEditingTunnel] = useState<Tunnel | null>(null)
  const [reapplyingAll, setReapplyingAll] = useState(false)
  const [showBench, setShowBench] = useState(false)
  const [benchIran, setBenchIran] = useState('')
  const [benchForeign, setBenchForeign] = useState('')
  const [benchRunning, setBenchRunning] = useState(false)
  const [benchError, setBenchError] = useState('')
  const [benchLogs, setBenchLogs] = useState<Array<{ ts: string; level: string; message: string }>>([])
  const [benchJobId, setBenchJobId] = useState('')
  const benchLogRef = useRef<HTMLDivElement | null>(null)
  const [benchResult, setBenchResult] = useState<{
    message: string
    best_port?: number
    best?: { core: string; type: string; port: number; upload_mbps: number; download_mbps: number } | null
    rows: Array<{ label: string; core: string; type: string; port?: number; ok: boolean; upload_mbps?: number; download_mbps?: number; error?: string }>
  } | null>(null)
  const [liveMap, setLiveMap] = useState<Record<string, TunnelLive>>({})
  const [liveLoading, setLiveLoading] = useState(false)

  const fetchLiveStatus = async () => {
    setLiveLoading(true)
    try {
      const response = await api.get('/tunnels/live-status')
      setLiveMap(response.data?.tunnels || {})
    } catch (error) {
      console.error('Failed to fetch live tunnel status:', error)
    } finally {
      setLiveLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
    const params = new URLSearchParams(window.location.search)
    if (params.get('create') === 'true') {
      setShowAddModal(true)
      window.history.replaceState({}, '', '/tunnels')
    }
  }, [])

  useEffect(() => {
    if (loading) return
    fetchLiveStatus()
    const timer = setInterval(fetchLiveStatus, 10000)
    return () => clearInterval(timer)
  }, [loading])

  const fetchData = async () => {
    try {
      const [tunnelsRes, nodesRes] = await Promise.all([
        api.get('/tunnels'),
        api.get('/nodes'),
      ])
      setTunnels(tunnelsRes.data)
      // Filter nodes: iran nodes and foreign servers
      const iranNodes = nodesRes.data.filter((node: any) => 
        node.metadata?.role === 'iran' || !node.metadata?.role  // Default to iran for backward compatibility
      )
      const foreignServers = nodesRes.data.filter((node: any) => 
        node.metadata?.role === 'foreign'
      )
      setNodes(iranNodes)
      setServers(foreignServers)
    } catch (error) {
      console.error('Failed to fetch data:', error)
    } finally {
      setLoading(false)
    }
  }

  const deleteTunnel = async (id: string) => {
    if (!confirm('Are you sure you want to delete this tunnel?')) return
    
    try {
      await api.delete(`/tunnels/${id}`)
      await fetchData()
      fetchLiveStatus()
    } catch (error) {
      console.error('Failed to delete tunnel:', error)
      alert(apiErrorMessage(error, 'Failed to delete tunnel'))
    }
  }

  const reapplyTunnel = async (tunnel: Tunnel) => {
    try {
      const response = await api.post(`/tunnels/${tunnel.id}/apply`)
      const st = response.data?.status
      if (st === 'success' || st === 'applied' || st === 'ok') {
        await fetchData()
        fetchLiveStatus()
      } else {
        throw new Error(response.data?.message || 'Failed to reapply tunnel')
      }
    } catch (error: any) {
      console.error('Failed to reapply tunnel:', error)
      alert(apiErrorMessage(error, 'Failed to reapply tunnel'))
    }
  }

  const handleReapplyAll = async () => {
    if (!confirm(t.tunnels.confirmReapplyAll || 'Are you sure you want to reapply all tunnels?')) return
    
    setReapplyingAll(true)
    try {
      const response = await api.post('/tunnels/reapply-all')
      if (response.data && response.data.status === 'success') {
        alert(`${t.tunnels.reapplyAllSuccess || 'Success'}: ${response.data.message}`)
        await fetchData()
        fetchLiveStatus()
      } else {
        throw new Error(response.data?.message || 'Failed to reapply all tunnels')
      }
    } catch (error: any) {
      console.error('Failed to reapply all tunnels:', error)
      alert(apiErrorMessage(error, 'Failed to reapply all tunnels'))
    } finally {
      setReapplyingAll(false)
    }
  }

  useEffect(() => {
    if (benchLogRef.current) {
      benchLogRef.current.scrollTop = benchLogRef.current.scrollHeight
    }
  }, [benchLogs])

  const runBench = async () => {
    setBenchRunning(true)
    setBenchError('')
    setBenchResult(null)
    setBenchLogs([{ ts: new Date().toISOString().slice(11, 19), level: 'info', message: 'Starting path test…' }])
    setBenchJobId('')
    try {
      const started = await api.post('/nodes/path-bench/start', {
        iran_node_id: benchIran || null,
        foreign_node_id: benchForeign || null,
      })
      const jobId = started.data?.job_id as string
      setBenchJobId(jobId)
      setBenchLogs((prev) => [...prev, { ts: new Date().toISOString().slice(11, 19), level: 'info', message: `Job ${jobId.slice(0, 8)} started on panel` }])

      for (let i = 0; i < 180; i++) {
        await new Promise((r) => setTimeout(r, 700))
        const status = await api.get(`/nodes/path-bench/jobs/${jobId}`)
        const logs = status.data?.logs || []
        setBenchLogs(logs)
        if (status.data?.state === 'done') {
          setBenchResult(status.data.result)
          break
        }
        if (status.data?.state === 'error') {
          setBenchError(status.data?.error || 'Path test failed. Existing tunnels were not changed.')
          break
        }
      }
    } catch (error) {
      setBenchError(apiErrorMessage(error, 'Path test failed. Existing tunnels were not changed.'))
    } finally {
      setBenchRunning(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <div className="text-center">
          <div className="inline-block animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 dark:border-blue-400 mb-4"></div>
          <p className="text-gray-500 dark:text-gray-400">{t.tunnels.loadingTunnels}</p>
        </div>
      </div>
    )
  }

  return (
    <div className="w-full max-w-7xl mx-auto px-1 sm:px-0">
      <div className="flex flex-col gap-4 sm:flex-row sm:justify-between sm:items-center mb-6 sm:mb-8">
        <div className="min-w-0">
          <h1 className="text-2xl sm:text-3xl font-bold text-gray-900 dark:text-white mb-2">{t.tunnels.title}</h1>
          <p className="text-sm sm:text-base text-gray-500 dark:text-gray-400">{t.tunnels.subtitle}</p>
        </div>
        <div className="flex flex-col sm:flex-row gap-2 sm:gap-3 w-full sm:w-auto">
          <button
            onClick={() => {
              setBenchError('')
              setBenchResult(null)
              setBenchLogs([])
              setBenchJobId('')
              setBenchIran(nodes[0]?.id || '')
              setBenchForeign(servers[0]?.id || '')
              setShowBench(true)
            }}
            className="w-full sm:w-auto justify-center px-5 py-2.5 bg-slate-800 text-white rounded-lg hover:bg-slate-900 font-medium shadow-sm flex items-center gap-2 text-sm sm:text-base"
          >
            Best tunnel test
          </button>
          <button
            onClick={handleReapplyAll}
            disabled={reapplyingAll}
            className="w-full sm:w-auto justify-center px-5 py-2.5 bg-gradient-to-r from-green-600 to-emerald-600 text-white rounded-lg hover:from-green-700 hover:to-emerald-700 transition-all duration-200 font-medium shadow-sm hover:shadow-md flex items-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed text-sm sm:text-base"
          >
            <RotateCw size={20} className={reapplyingAll ? "animate-spin" : ""} />
            {t.tunnels.reapplyAll}
          </button>
          <button
            onClick={() => setShowAddModal(true)}
            className="w-full sm:w-auto justify-center px-5 py-2.5 bg-gradient-to-r from-blue-600 to-indigo-600 text-white rounded-lg hover:from-blue-700 hover:to-indigo-700 transition-all duration-200 font-medium shadow-sm hover:shadow-md flex items-center gap-2 text-sm sm:text-base"
          >
            <Plus size={20} />
            {t.tunnels.createTunnel}
          </button>
        </div>
      </div>

      <div className="space-y-3">
        {tunnels.map((tunnel) => {
          // Extract ports from spec
          const getPorts = (): string => {
            if (tunnel.spec?.ports) {
              if (Array.isArray(tunnel.spec.ports)) {
                // For Backhaul, ports are in format "8080=127.0.0.1:8080", extract just the port numbers
                if (tunnel.core === 'backhaul' && typeof tunnel.spec.ports[0] === 'string' && tunnel.spec.ports[0].includes('=')) {
                  return tunnel.spec.ports.map(p => {
                    const portPart = p.split('=')[0]
                    const port = portPart.includes(':') ? portPart.split(':')[1] : portPart
                    return port
                  }).join(', ')
                }
                // For other cores, ports are numbers
                return tunnel.spec.ports.map(p => typeof p === 'object' && p.local ? p.local : p).join(', ')
              } else if (typeof tunnel.spec.ports === 'string') {
                return tunnel.spec.ports
              }
            }
            // Fallback to single port
            const port = tunnel.spec?.listen_port || tunnel.spec?.remote_port
            return port ? port.toString() : 'N/A'
          }

          // Get core badge color
          const getCoreBadge = () => {
            const coreColors: Record<string, { bg: string; text: string; border: string }> = {
              rathole: { bg: 'bg-purple-100 dark:bg-purple-900/30', text: 'text-purple-800 dark:text-purple-200', border: 'border-purple-300 dark:border-purple-700' },
              backhaul: { bg: 'bg-blue-100 dark:bg-blue-900/30', text: 'text-blue-800 dark:text-blue-200', border: 'border-blue-300 dark:border-blue-700' },
              chisel: { bg: 'bg-orange-100 dark:bg-orange-900/30', text: 'text-orange-800 dark:text-orange-200', border: 'border-orange-300 dark:border-orange-700' },
              frp: { bg: 'bg-cyan-100 dark:bg-cyan-900/30', text: 'text-cyan-800 dark:text-cyan-200', border: 'border-cyan-300 dark:border-cyan-700' },
              wstunnel: { bg: 'bg-teal-100 dark:bg-teal-900/30', text: 'text-teal-800 dark:text-teal-200', border: 'border-teal-300 dark:border-teal-700' },
              bore: { bg: 'bg-lime-100 dark:bg-lime-900/30', text: 'text-lime-800 dark:text-lime-200', border: 'border-lime-300 dark:border-lime-700' },
              gost: { bg: 'bg-indigo-100 dark:bg-indigo-900/30', text: 'text-indigo-800 dark:text-indigo-200', border: 'border-indigo-300 dark:border-indigo-700' },
            }
            return coreColors[tunnel.core] || { bg: 'bg-gray-100 dark:bg-gray-700', text: 'text-gray-800 dark:text-gray-200', border: 'border-gray-300 dark:border-gray-600' }
          }

          const coreBadge = getCoreBadge()
          const ports = getPorts()
          const iranNode = nodes.find(n => n.id === tunnel.iran_node_id || n.id === tunnel.node_id)
          const foreignServer = servers.find(s => s.id === tunnel.foreign_node_id)
          const live = liveMap[tunnel.id]
          const displayStatus = live?.status || (liveLoading ? 'checking' : tunnel.status)
          const statusLabel =
            displayStatus === 'live' || displayStatus === 'active'
              ? 'live'
              : displayStatus === 'offline'
              ? 'offline'
              : displayStatus === 'checking'
              ? 'checking'
              : 'error'
          const statusClass =
            statusLabel === 'live'
              ? 'bg-green-100 dark:bg-green-900/30 text-green-800 dark:text-green-200'
              : statusLabel === 'offline'
              ? 'bg-gray-200 dark:bg-gray-700 text-gray-800 dark:text-gray-200'
              : statusLabel === 'checking'
              ? 'bg-amber-100 dark:bg-amber-900/30 text-amber-800 dark:text-amber-200'
              : 'bg-red-100 dark:bg-red-900/30 text-red-800 dark:text-red-200'
          const portRows: PortLive[] =
            live?.ports?.length
              ? live.ports
              : ports
                  .split(',')
                  .map((p) => p.trim())
                  .filter(Boolean)
                  .map((p) => ({
                    port: parseInt(p, 10) || 0,
                    status: liveLoading ? 'checking' : 'offline',
                    error: liveLoading ? null : 'Waiting for live probe',
                  }))

          return (
            <div
              key={tunnel.id}
              className="bg-white dark:bg-gray-800 rounded-xl shadow-sm border border-gray-200 dark:border-gray-700 p-4 sm:p-5 transition-all hover:shadow-lg hover:border-gray-300 dark:hover:border-gray-600 overflow-hidden"
            >
              <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-3 sm:gap-4">
                <div className="flex items-start gap-3 sm:gap-4 flex-1 min-w-0">
                  {/* Status Badge — from real listen probe when available */}
                  <span
                    className={`px-3 py-1.5 rounded-full text-xs font-semibold whitespace-nowrap shrink-0 ${statusClass}`}
                    title={live?.error || tunnel.error_message || ''}
                  >
                    {statusLabel}
                  </span>

                  <div className="flex-1 min-w-0">
                    {/* Name, Core Badge, Transmission Badge, and Ports in one line */}
                    <div className="flex items-center gap-2 sm:gap-3 mb-2 flex-wrap break-words">
                      <h3 className="text-base font-semibold text-gray-900 dark:text-white break-all min-w-0 max-w-full">{tunnel.name}</h3>
                      <span
                        className={`px-3 py-1.5 rounded-lg text-xs font-bold uppercase tracking-wide border ${coreBadge.bg} ${coreBadge.text} ${coreBadge.border} shrink-0`}
                      >
                        {tunnel.core}
                      </span>
                      {(() => {
                        let transmissionType = null
                        if (tunnel.core === 'chisel') {
                          transmissionType = 'TCP'
                        } else if (tunnel.core === 'wstunnel') {
                          transmissionType = (tunnel.type || 'tcp').toUpperCase()
                        } else if (tunnel.core === 'bore') {
                          transmissionType = 'TCP'
                        } else if (tunnel.core === 'rathole') {
                          const transport = tunnel.spec?.transport || (tunnel.type && tunnel.type !== 'rathole' ? tunnel.type : 'tcp')
                          transmissionType = transport.toUpperCase()
                        } else if (tunnel.type && tunnel.type.toLowerCase() !== tunnel.core.toLowerCase()) {
                          transmissionType = tunnel.type.toUpperCase()
                        }
                        
                        if (!transmissionType) return null
                        
                        const getTransmissionBadge = () => {
                          const typeColors: Record<string, { bg: string; text: string; border: string }> = {
                            TCP: { bg: 'bg-green-100 dark:bg-green-900/30', text: 'text-green-800 dark:text-green-200', border: 'border-green-300 dark:border-green-700' },
                            UDP: { bg: 'bg-yellow-100 dark:bg-yellow-900/30', text: 'text-yellow-800 dark:text-yellow-200', border: 'border-yellow-300 dark:border-yellow-700' },
                            WS: { bg: 'bg-pink-100 dark:bg-pink-900/30', text: 'text-pink-800 dark:text-pink-200', border: 'border-pink-300 dark:border-pink-700' },
                            WSS: { bg: 'bg-pink-100 dark:bg-pink-900/30', text: 'text-pink-800 dark:text-pink-200', border: 'border-pink-300 dark:border-pink-700' },
                            GRPC: { bg: 'bg-teal-100 dark:bg-teal-900/30', text: 'text-teal-800 dark:text-teal-200', border: 'border-teal-300 dark:border-teal-700' },
                            TCPMUX: { bg: 'bg-violet-100 dark:bg-violet-900/30', text: 'text-violet-800 dark:text-violet-200', border: 'border-violet-300 dark:border-violet-700' },
                          }
                          return typeColors[transmissionType] || { bg: 'bg-gray-100 dark:bg-gray-700', text: 'text-gray-800 dark:text-gray-200', border: 'border-gray-300 dark:border-gray-600' }
                        }
                        
                        const transmissionBadge = getTransmissionBadge()
                        return (
                          <span
                            className={`px-3 py-1.5 rounded-lg text-xs font-bold uppercase tracking-wide border ${transmissionBadge.bg} ${transmissionBadge.text} ${transmissionBadge.border} shrink-0`}
                          >
                            {transmissionType}
                          </span>
                        )
                      })()}
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <span className="text-xs font-medium text-gray-500 dark:text-gray-400">Ports:</span>
                        {portRows.map((row) => {
                          const st = row.status === 'live' ? 'live' : row.status === 'checking' ? 'checking' : row.status === 'offline' ? 'offline' : 'error'
                          const chip =
                            st === 'live'
                              ? 'bg-green-50 dark:bg-green-900/20 text-green-800 dark:text-green-200 border-green-300 dark:border-green-700'
                              : st === 'checking'
                              ? 'bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-200 border-amber-300 dark:border-amber-700'
                              : st === 'offline'
                              ? 'bg-gray-50 dark:bg-gray-700/40 text-gray-700 dark:text-gray-300 border-gray-300 dark:border-gray-600'
                              : 'bg-red-50 dark:bg-red-900/20 text-red-800 dark:text-red-200 border-red-300 dark:border-red-700'
                          return (
                            <span
                              key={`${tunnel.id}-${row.port}`}
                              title={row.error || st}
                              className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs font-mono border ${chip}`}
                            >
                              <span
                                className={`inline-block w-1.5 h-1.5 rounded-full ${
                                  st === 'live' ? 'bg-green-500' : st === 'checking' ? 'bg-amber-500' : st === 'offline' ? 'bg-gray-400' : 'bg-red-500'
                                }`}
                              />
                              {row.port || '?'}
                              <span className="font-sans font-semibold uppercase tracking-wide">{st}</span>
                            </span>
                          )
                        })}
                      </div>
                    </div>

                    {/* Core Port, Node and Server Info */}
                    <div className="flex items-center gap-x-4 gap-y-1.5 text-xs text-gray-500 dark:text-gray-400 flex-wrap break-words">
                      {(() => {
                        let corePort = null
                        if (tunnel.core === 'rathole') {
                          if (tunnel.spec?.bind_addr) {
                            const match = tunnel.spec.bind_addr.match(/:(\d+)$/)
                            if (match) corePort = match[1]
                          }
                          if (!corePort && tunnel.spec?.control_port) {
                            corePort = tunnel.spec.control_port
                          }
                          if (!corePort) {
                            const remoteAddr = tunnel.spec?.remote_addr || ''
                            const match = remoteAddr.match(/:(\d+)$/)
                            if (match) corePort = match[1]
                          }
                          if (!corePort) corePort = '23333'
                        } else if (tunnel.core === 'chisel') {
                          corePort = tunnel.spec?.control_port || tunnel.spec?.server_port
                        } else if (tunnel.core === 'wstunnel') {
                          corePort = tunnel.spec?.control_port || tunnel.spec?.server_port
                        } else if (tunnel.core === 'bore') {
                          corePort = tunnel.spec?.control_port || '7835'
                        } else if (tunnel.core === 'backhaul') {
                          corePort = tunnel.spec?.control_port || tunnel.spec?.public_port || '3080'
                        } else if (tunnel.core === 'frp') {
                          corePort = tunnel.spec?.bind_port || '7000'
                        }
                        return corePort ? (
                          <div className="flex items-center gap-1.5">
                            <span className="font-medium">Core Port:</span>
                            <span className="text-gray-700 dark:text-gray-300 font-mono">{corePort}</span>
                          </div>
                        ) : null
                      })()}
                      {iranNode && (
                        <div className="flex items-center gap-1.5">
                          <span className="font-medium">Node:</span>
                          <span className="text-gray-700 dark:text-gray-300">{iranNode.name || iranNode.id.substring(0, 8)}</span>
                        </div>
                      )}
                      {foreignServer && (
                        <div className="flex items-center gap-1.5">
                          <span className="font-medium">Server:</span>
                          <span className="text-gray-700 dark:text-gray-300">{foreignServer.name || foreignServer.id.substring(0, 8)}</span>
                        </div>
                      )}
                    </div>

                    {/* Error Message — prefer live probe text */}
                    {(live?.error || tunnel.error_message) && statusLabel !== 'live' && (
                      <div className="mt-2 text-xs text-red-600 dark:text-red-400 break-words overflow-hidden">
                        {live?.error || tunnel.error_message}
                      </div>
                    )}
                    {live?.ports?.some((p) => p.status !== 'live' && p.error) && (
                      <div className="mt-1 space-y-0.5 break-words">
                        {live.ports
                          .filter((p) => p.status !== 'live' && p.error)
                          .map((p) => (
                            <div key={`err-${tunnel.id}-${p.port}`} className="text-xs text-red-600 dark:text-red-400 break-words">
                              Port {p.port}: {p.error}
                            </div>
                          ))}
                      </div>
                    )}
                  </div>
                </div>

                {/* Action Buttons */}
                <div className="flex sm:flex-col lg:flex-row gap-2 shrink-0 justify-end sm:justify-start border-t sm:border-t-0 border-gray-100 dark:border-gray-700 pt-3 sm:pt-0">
                  <button
                    onClick={() => reapplyTunnel(tunnel)}
                    className="p-2.5 text-green-600 hover:bg-green-50 dark:hover:bg-green-900/20 rounded-lg transition-colors"
                    title="Reapply tunnel"
                  >
                    <RotateCw size={18} />
                  </button>
                  <button
                    onClick={() => setEditingTunnel(tunnel)}
                    className="p-2.5 text-blue-600 hover:bg-blue-50 dark:hover:bg-blue-900/20 rounded-lg transition-colors"
                    title="Edit tunnel"
                  >
                    <Edit2 size={18} />
                  </button>
                  <button
                    onClick={() => deleteTunnel(tunnel.id)}
                    className="p-2.5 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg transition-colors"
                    title="Delete tunnel"
                  >
                    <Trash2 size={18} />
                  </button>
                </div>
              </div>
            </div>
          )
        })}
      </div>

      {showBench && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-[100] p-3 sm:p-4 overflow-y-auto">
          <div className="bg-white dark:bg-gray-800 rounded-lg p-4 sm:p-6 w-full max-w-3xl max-h-[90vh] overflow-y-auto my-auto">
            <h2 className="text-xl font-bold text-gray-900 dark:text-white mb-1">Best tunnel test</h2>
            <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
              Measures real TCP upload and download between the selected servers, then compares GOST and FRP on temporary ports. Existing tunnels are not changed. Only one test can run at a time.
            </p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-4">
              <label className="text-sm text-gray-700 dark:text-gray-300">
                Iran node
                <select
                  value={benchIran}
                  onChange={(e) => setBenchIran(e.target.value)}
                  disabled={benchRunning}
                  className="mt-1 w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white disabled:opacity-60"
                >
                  {nodes.map((node) => (
                    <option key={node.id} value={node.id}>{node.name}</option>
                  ))}
                </select>
              </label>
              <label className="text-sm text-gray-700 dark:text-gray-300">
                Foreign server
                <select
                  value={benchForeign}
                  onChange={(e) => setBenchForeign(e.target.value)}
                  disabled={benchRunning}
                  className="mt-1 w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white disabled:opacity-60"
                >
                  {servers.map((server) => (
                    <option key={server.id} value={server.id}>{server.name}</option>
                  ))}
                </select>
              </label>
            </div>

            <div className="mb-4">
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-medium text-gray-500 dark:text-gray-400">
                  Server log {benchJobId ? `(job ${benchJobId.slice(0, 8)})` : ''}
                </span>
                {benchRunning && <span className="text-xs text-amber-600 dark:text-amber-400">running…</span>}
              </div>
              <div
                ref={benchLogRef}
                className="h-56 overflow-y-auto rounded-lg bg-gray-950 text-gray-100 font-mono text-xs p-3 border border-gray-800"
              >
                {benchLogs.length === 0 ? (
                  <div className="text-gray-500">Press Run test to see live progress from the panel and nodes.</div>
                ) : (
                  benchLogs.map((line, idx) => (
                    <div
                      key={`${line.ts}-${idx}`}
                      className={
                        line.level === 'error'
                          ? 'text-red-400'
                          : line.level === 'warn'
                          ? 'text-amber-300'
                          : 'text-gray-200'
                      }
                    >
                      <span className="text-gray-500">[{line.ts}]</span> {line.message}
                    </div>
                  ))
                )}
              </div>
            </div>

            {benchError && <p className="text-sm text-red-600 dark:text-red-400 mb-3">{benchError}</p>}
            {benchResult && (
              <div className="mb-4">
                <p className="text-sm text-teal-800 dark:text-teal-200 mb-3">{benchResult.message}</p>
                <div className="overflow-x-auto -mx-1 px-1"><table className="w-full text-sm min-w-[480px]">
                  <thead>
                    <tr className="text-left text-gray-500 dark:text-gray-400">
                      <th className="py-1">Test</th>
                      <th>Type</th>
                      <th>Upload</th>
                      <th>Download</th>
                    </tr>
                  </thead>
                  <tbody>
                    {benchResult.rows.map((row) => (
                      <tr key={row.label} className="border-t border-gray-100 dark:border-gray-700 text-gray-800 dark:text-gray-100">
                        <td className="py-2">{row.label}</td>
                        <td>{row.type.toUpperCase()}</td>
                        <td>{row.ok ? `${row.upload_mbps} Mbps` : row.error}</td>
                        <td>{row.ok ? `${row.download_mbps} Mbps` : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                </div>
              </div>
            )}
            <div className="flex flex-col-reverse sm:flex-row gap-2 sm:gap-3 justify-end">
              <button
                type="button"
                onClick={() => setShowBench(false)}
                className="px-4 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200 rounded-lg"
              >
                Close
              </button>
              <button
                type="button"
                onClick={runBench}
                disabled={benchRunning || !benchIran || !benchForeign}
                className="px-4 py-2 bg-slate-800 text-white rounded-lg disabled:opacity-60"
              >
                {benchRunning ? 'Testing…' : 'Run test'}
              </button>
            </div>
          </div>
        </div>
      )}

      {showAddModal && (
        <AddTunnelModal
          nodes={nodes}
          servers={servers}
          onClose={() => setShowAddModal(false)}
          onSuccess={() => {
            setShowAddModal(false)
            fetchData().then(() => fetchLiveStatus())
          }}
        />
      )}

      {editingTunnel && (
        <EditTunnelModal
          tunnel={editingTunnel}
          nodes={nodes}
          onClose={() => setEditingTunnel(null)}
          onSuccess={() => {
            setEditingTunnel(null)
            fetchData().then(() => fetchLiveStatus())
          }}
        />
      )}
    </div>
  )
}

interface EditTunnelModalProps {
  tunnel: Tunnel
  nodes: any[]
  onClose: () => void
  onSuccess: () => void
}

const EditTunnelModal = ({ tunnel, onClose, onSuccess }: EditTunnelModalProps) => {
  const { t } = useLanguage()
  const forwardToParsed = tunnel.spec?.forward_to ? parseAddressPort(tunnel.spec.forward_to) : null
  const remoteIp = tunnel.spec?.remote_ip || forwardToParsed?.host || '127.0.0.1'
  const remotePort = tunnel.spec?.remote_port || forwardToParsed?.port || 8080
  
  // Parse ports from spec
  const parsePortsFromSpec = (spec: Record<string, any>): string => {
    if (spec?.ports) {
      if (Array.isArray(spec.ports)) {
        // For Backhaul, ports are in format "8080=127.0.0.1:8080" or "0.0.0.0:8080=127.0.0.1:8080"
        // Extract just the port number (first number before = or after :)
        return spec.ports.map(p => {
          if (typeof p === 'object' && p.local) {
            return p.local.toString()
          } else if (typeof p === 'string') {
            // Handle Backhaul format: "8080=127.0.0.1:8080" or "0.0.0.0:8080=127.0.0.1:8080"
            if (p.includes('=')) {
              const leftPart = p.split('=')[0]
              // Extract port from left part (could be "8080" or "0.0.0.0:8080")
              if (leftPart.includes(':')) {
                return leftPart.split(':')[1]
              }
              return leftPart
            }
            // If it's just a number, return as-is
            return p
          }
          return p.toString()
        }).join(',')
      } else if (typeof spec.ports === 'string') {
        return spec.ports
      }
    }
    // Fallback to single port
    return (spec?.listen_port || spec?.remote_port || 8080).toString()
  }
  
  const [formData, setFormData] = useState({
    name: tunnel.name,
    ports: parsePortsFromSpec(tunnel.spec || {}),
    remote_ip: remoteIp,
    rathole_remote_addr: tunnel.spec?.remote_addr ? (() => {
      const parsed = parseAddressPort(tunnel.spec.remote_addr)
      return parsed.port?.toString() || ''
    })() : '',
    chisel_control_port: tunnel.spec?.control_port ? tunnel.spec.control_port.toString() : '',
    wstunnel_control_port: tunnel.core === 'wstunnel' && tunnel.spec?.control_port ? tunnel.spec.control_port.toString() : '',
    frp_bind_port: tunnel.spec?.bind_port ? tunnel.spec.bind_port.toString() : '7000',
    frp_token: tunnel.spec?.token || '',
    frp_local_ip: tunnel.spec?.local_ip || '127.0.0.1',
    node_ipv6: tunnel.spec?.node_ipv6 || '',
  })
  const parsedBackhaul = parseBackhaulSpec(tunnel.spec, tunnel.type)
  const [backhaulState, setBackhaulState] = useState<BackhaulFormState>(parsedBackhaul.state)
  const [backhaulAdvanced, setBackhaulAdvanced] = useState<BackhaulAdvancedState>(parsedBackhaul.advanced)
  const [showBackhaulAdvanced, setShowBackhaulAdvanced] = useState(false)
  const submittingRef = useRef(false)
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [core, setCore] = useState(tunnel.core)
  const [tunnelType, setTunnelType] = useState(
    tunnel.core === 'chisel' || tunnel.core === 'bore' ? 'tcp' : (tunnel.type || 'tcp')
  )

  const typesForCore = (value: string): string[] => {
    if (value === 'rathole') return ['tcp', 'ws']
    if (value === 'frp' || value === 'wstunnel') return ['tcp', 'udp']
    if (value === 'backhaul') return ['tcp', 'udp', 'ws', 'wsmux', 'tcpmux']
    if (value === 'chisel' || value === 'bore') return ['tcp']
    return ['tcp', 'udp', 'grpc', 'tcpmux']
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (submittingRef.current) {
      return
    }
    submittingRef.current = true
    setSubmitting(true)
    setFormError(null)
    try {
      let updatedSpec = core === tunnel.core ? { ...tunnel.spec } : {}
      
      const useV4ToV6 = updatedSpec.use_ipv6 || false
      
      // Parse comma-separated ports
      const parsePorts = (portsStr: string): number[] => {
        return portsStr
          .split(',')
          .map(p => p.trim())
          .filter(p => p)
          .map(p => parseInt(p))
          .filter(p => !isNaN(p) && p > 0 && p <= 65535)
      }
      
      const ports = parsePorts(formData.ports)
      if (ports.length === 0) {
        alert('Please enter at least one valid port')
        submittingRef.current = false
        setSubmitting(false)
        return
      }
      
      if (core === 'rathole') {
        if (formData.rathole_remote_addr) {
          const remoteHost = window.location.hostname
          const remotePort = formData.rathole_remote_addr.includes(':') 
            ? formData.rathole_remote_addr.split(':')[1] 
            : formData.rathole_remote_addr
          updatedSpec.remote_addr = `${remoteHost}:${remotePort || '23333'}`
        }
        if (formData.node_ipv6) {
          updatedSpec.node_ipv6 = formData.node_ipv6
        }
        updatedSpec.ports = ports
        updatedSpec.remote_port = ports[0]  // Keep for backward compatibility
        updatedSpec.listen_port = ports[0]  // Keep for backward compatibility
      } else if (core === 'gost') {
        delete updatedSpec.forward_to
        updatedSpec.type = tunnelType
        const remoteIp = formData.remote_ip || '127.0.0.1'
        updatedSpec.remote_ip = remoteIp
        updatedSpec.ports = ports
        updatedSpec.remote_port = ports[0]  // Keep for backward compatibility
        updatedSpec.listen_port = ports[0]  // Keep for backward compatibility
      } else if (core === 'chisel') {
        updatedSpec.ports = ports
        const firstPort = ports[0]
        updatedSpec.listen_port = firstPort
        updatedSpec.remote_port = firstPort
        const controlPort = formData.chisel_control_port 
          ? parseInt(formData.chisel_control_port.toString())
          : firstPort + 10000
        updatedSpec.control_port = controlPort
        if (formData.node_ipv6) {
          updatedSpec.node_ipv6 = formData.node_ipv6
        }
      } else if (core === 'wstunnel') {
        updatedSpec.ports = ports
        const firstPort = ports[0]
        updatedSpec.listen_port = firstPort
        updatedSpec.remote_port = firstPort
        const controlPort = formData.wstunnel_control_port
          ? parseInt(formData.wstunnel_control_port.toString())
          : firstPort + 10000
        updatedSpec.control_port = controlPort
        updatedSpec.type = tunnelType === 'udp' ? 'udp' : 'tcp'
        updatedSpec.local_addr = `127.0.0.1:${firstPort}`
      } else if (core === 'bore') {
        updatedSpec.ports = ports
        const firstPort = ports[0]
        updatedSpec.listen_port = firstPort
        updatedSpec.remote_port = firstPort
        updatedSpec.control_port = 7835
        updatedSpec.type = 'tcp'
        updatedSpec.local_host = '127.0.0.1'
      } else if (core === 'frp') {
        const bindPort = parseInt(formData.frp_bind_port) || 7000
        updatedSpec.bind_port = bindPort
        updatedSpec.ports = ports
        updatedSpec.listen_port = ports[0]  // Keep for backward compatibility
        updatedSpec.remote_port = ports[0]  // Keep for backward compatibility
        if (formData.frp_token) {
          updatedSpec.token = formData.frp_token
        } else {
          delete updatedSpec.token
        }
        updatedSpec.local_ip = formData.frp_local_ip || '127.0.0.1'
        updatedSpec.local_port = ports[0]  // Keep for backward compatibility
        updatedSpec.type = tunnelType === 'udp' ? 'udp' : 'tcp'
      } else if (core === 'backhaul') {
        updatedSpec = buildBackhaulSpec(backhaulState, backhaulAdvanced, tunnelType as BackhaulTransport)
        // Override ports if provided
        if (ports.length > 0) {
          const targetHost = updatedSpec.target_host || '127.0.0.1'
          updatedSpec.ports = ports.map(p => `${p}=${targetHost}:${p}`)
        }
      }

      await api.put(`/tunnels/${tunnel.id}`, {
        name: formData.name,
        core,
        type: core === 'backhaul' ? backhaulState.transport : tunnelType,
        spec: updatedSpec,
      })
      onSuccess()
    } catch (error) {
      console.error('Failed to update tunnel:', error)
      setFormError(apiErrorMessage(error, 'Failed to update tunnel'))
      submittingRef.current = false
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-[100] p-3 sm:p-4 overflow-y-auto">
      <div className="bg-white dark:bg-gray-800 rounded-lg p-4 sm:p-6 w-full max-w-lg max-h-[90vh] overflow-y-auto my-auto">
        <h2 className="text-xl font-bold text-gray-900 dark:text-white mb-1">Edit Tunnel</h2>
        <p className="text-sm text-gray-500 dark:text-gray-400 mb-4">
          Saving applies the new port, type, and core immediately. The previous process for this tunnel is replaced, not duplicated.
        </p>
        <form onSubmit={handleSubmit} className="space-y-4">
          {formError && (
            <p className="text-sm text-red-600 dark:text-red-400">{formError}</p>
          )}
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              {t.tunnels.name}
            </label>
            <input
              type="text"
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
              required
            />
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Core</label>
              <select
                value={core}
                onChange={(e) => {
                  const next = e.target.value
                  const allowed = typesForCore(next)
                  setCore(next)
                  setTunnelType(allowed.includes(tunnelType) ? tunnelType : allowed[0])
                  if (next === 'backhaul') {
                    setBackhaulState((prev) => ({ ...prev, transport: (allowed.includes(tunnelType) ? tunnelType : allowed[0]) as BackhaulTransport }))
                  }
                }}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
              >
                <option value="gost">GOST</option>
                <option value="frp">FRP</option>
                <option value="bore">Bore</option>
                <option value="chisel">Chisel</option>
                <option value="wstunnel">Wstunnel</option>
                <option value="rathole">Rathole</option>
                <option value="backhaul">Backhaul</option>
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Type</label>
              <select
                value={typesForCore(core).includes(tunnelType) ? tunnelType : typesForCore(core)[0]}
                onChange={(e) => {
                  const value = e.target.value
                  setTunnelType(value)
                  if (core === 'backhaul') {
                    setBackhaulState((prev) => ({ ...prev, transport: value as BackhaulTransport }))
                  }
                }}
                disabled={core === 'chisel' || core === 'bore'}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white disabled:opacity-70"
              >
                {typesForCore(core).map((item) => (
                  <option key={item} value={item}>{item.toUpperCase()}</option>
                ))}
              </select>
            </div>
          </div>
          {core === 'gost' && (
            <>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  {t.tunnels.remoteIP}
                </label>
                <input
                  type="text"
                  value={formData.remote_ip}
                  onChange={(e) =>
                    setFormData({ ...formData, remote_ip: e.target.value || '127.0.0.1' })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder="127.0.0.1 or [2001:db8::1]"
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  {t.tunnels.remoteIPDescription}
                </p>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Ports
                </label>
                <input
                  type="text"
                  value={formData.ports}
                  onChange={(e) =>
                    setFormData({ ...formData, ports: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder="8080,8081,8082"
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Ports (comma-separated, same for panel and target server)
                </p>
              </div>
            </>
          )}
          
          {core === 'backhaul' && (
            <BackhaulForm
              state={backhaulState}
              onChange={(partial) => {
                setBackhaulState((prev) => ({ ...prev, ...partial }))
              }}
              onOpenAdvanced={() => setShowBackhaulAdvanced(true)}
              acceptUdpVisible={
                backhaulState.transport === 'tcp' || backhaulState.transport === 'tcpmux'
              }
            />
          )}
          
          {core === 'rathole' && (
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Ports
              </label>
              <input
                type="text"
                value={formData.ports}
                onChange={(e) =>
                  setFormData({ ...formData, ports: e.target.value })
                }
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                placeholder="8080,8081,8082"
              />
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                Ports (comma-separated, same for panel and node local service)
              </p>
            </div>
          )}
          
          {core === 'rathole' && (
            <>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Rathole Port
                </label>
                <input
                  type="number"
                  value={formData.rathole_remote_addr ? formData.rathole_remote_addr.split(':')[1] || formData.rathole_remote_addr : ''}
                  onChange={(e) => {
                    const port = e.target.value
                    const host = window.location.hostname
                    setFormData({ ...formData, rathole_remote_addr: port ? `${host}:${port}` : '' })
                  }}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder="23333"
                  min="1"
                  max="65535"
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Rathole server port on panel (IP: {window.location.hostname})</p>
              </div>
            </>
          )}
          
          {core === 'chisel' && (
            <>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Ports
                </label>
                <input
                  type="text"
                  value={formData.ports}
                  onChange={(e) =>
                    setFormData({ ...formData, ports: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder="8080,8081,8082"
                  required
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Ports (comma-separated, same for reverse port and local port)
                </p>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Control Port
                </label>
                <input
                  type="number"
                  value={formData.chisel_control_port}
                  onChange={(e) =>
                    setFormData({ ...formData, chisel_control_port: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder={`${(parseInt(formData.ports.split(',')[0]?.trim()) || 8080) + 10000} (auto)`}
                  min="1"
                  max="65535"
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Chisel server control port (leave empty for auto: first port + 10000)
                </p>
              </div>
              {/* Node IPv6 address field for Chisel when v4 to v6 is enabled */}
              {tunnel.spec?.use_ipv6 && (
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Node IPv6 Address (Optional)
                  </label>
                  <input
                    type="text"
                    value={formData.node_ipv6}
                    onChange={(e) =>
                      setFormData({ ...formData, node_ipv6: e.target.value })
                    }
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                    placeholder="::1 or 2001:db8::1"
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    IPv6 address of the node. Leave empty to use ::1 (localhost IPv6)
                  </p>
                </div>
              )}
            </>
          )}

          {core === 'wstunnel' && (
            <>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Ports
                </label>
                <input
                  type="text"
                  value={formData.ports}
                  onChange={(e) =>
                    setFormData({ ...formData, ports: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder="8080,8081,8082"
                  required
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Public ports on Iran node (forwarded to same ports on foreign)
                </p>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Control Port
                </label>
                <input
                  type="number"
                  value={formData.wstunnel_control_port}
                  onChange={(e) =>
                    setFormData({ ...formData, wstunnel_control_port: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder={`${(parseInt(formData.ports.split(',')[0]?.trim()) || 8080) + 10000} (auto)`}
                  min="1"
                  max="65535"
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Wstunnel WebSocket control port (leave empty for auto: first port + 10000)
                </p>
              </div>
            </>
          )}

          {core === 'bore' && (
            <>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Ports
                </label>
                <input
                  type="text"
                  value={formData.ports}
                  onChange={(e) =>
                    setFormData({ ...formData, ports: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder="8080,8081,8082"
                  required
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Public TCP ports on Iran (forwarded to same ports on foreign)
                </p>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Control Port
                </label>
                <input
                  type="number"
                  value={7835}
                  disabled
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white opacity-70"
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Bore control port is fixed at 7835 (one shared server per Iran node)
                </p>
              </div>
            </>
          )}
          
          {core === 'frp' && (
            <>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Bind Port
                  </label>
                  <input
                    type="number"
                    value={formData.frp_bind_port}
                    onChange={(e) =>
                      setFormData({ ...formData, frp_bind_port: e.target.value })
                    }
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                    placeholder="7000"
                    min="1"
                    max="65535"
                    required
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    FRP server port on panel (default: 7000)
                  </p>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Ports
                  </label>
                  <input
                    type="text"
                    value={formData.ports}
                    onChange={(e) =>
                      setFormData({ ...formData, ports: e.target.value })
                    }
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                    placeholder="8080,8081,8082"
                    required
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Ports (comma-separated, same for remote port and local port)
                  </p>
                </div>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Token (Optional - Auto-generated if empty)
                </label>
                <input
                  type="text"
                  value={formData.frp_token}
                  onChange={(e) =>
                    setFormData({ ...formData, frp_token: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder="Leave empty for auto-generation"
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Authentication token (will be auto-generated if not provided)</p>
              </div>
            </>
          )}
          
          {/* Node IPv6 address field for Rathole when v4 to v6 is enabled */}
          {core === 'rathole' && tunnel.spec?.use_ipv6 && (
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Node IPv6 Address (Optional)
              </label>
              <input
                type="text"
                value={formData.node_ipv6}
                onChange={(e) =>
                  setFormData({ ...formData, node_ipv6: e.target.value })
                }
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                placeholder="::1 or 2001:db8::1"
              />
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                IPv6 address of the node. Leave empty to use ::1 (localhost IPv6)
              </p>
            </div>
          )}
          
          <div className="flex flex-col-reverse sm:flex-row gap-2 sm:gap-3 justify-end">
            <button
              type="button"
              onClick={onClose}
              disabled={submitting}
              className="px-4 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-600 disabled:opacity-50"
            >
              {t.tunnels.cancel}
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {submitting ? 'Saving…' : 'Save Changes'}
            </button>
          </div>
        </form>
        <BackhaulAdvancedDrawer
          open={showBackhaulAdvanced}
          state={backhaulAdvanced}
          onClose={() => setShowBackhaulAdvanced(false)}
          onChange={setBackhaulAdvanced}
        />
      </div>
    </div>
  )
}

interface AddTunnelModalProps {
  nodes: any[]
  servers: any[]
  onClose: () => void
  onSuccess: () => void
}

const AddTunnelModal = ({ nodes, servers, onClose, onSuccess }: AddTunnelModalProps) => {
  const { t } = useLanguage()
  const submittingRef = useRef(false)
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [formData, setFormData] = useState({
    name: '',
    core: 'gost',
    type: 'tcp',
    node_id: '',
    foreign_node_id: '',
    iran_node_id: '',
    ports: '8080',  // Comma-separated ports (e.g., "8080,8081,8082")
    remote_ip: '127.0.0.1',
    rathole_remote_addr: '23333',
    rathole_token: '',
    chisel_control_port: '',  // Empty means auto (listen_port + 10000)
    wstunnel_control_port: '',
    frp_bind_port: '7000',
    frp_token: '',
    frp_local_ip: '127.0.0.1',
    use_ipv6: false,
    node_ipv6: '',  // Optional IPv6 address for node (Rathole/Chisel)
    spec: {} as Record<string, any>,
  })
  const [backhaulState, setBackhaulState] = useState<BackhaulFormState>(createDefaultBackhaulState())
  const [backhaulAdvanced, setBackhaulAdvanced] = useState<BackhaulAdvancedState>(createDefaultBackhaulAdvancedState())
  const [showBackhaulAdvanced, setShowBackhaulAdvanced] = useState(false)

  // Auto-populate remote_ip: prefer GRE peer_inner when Iran↔Foreign GRE exists
  useEffect(() => {
    if (formData.core === 'gost' && formData.foreign_node_id) {
      const selectedServer = servers.find(s => s.id === formData.foreign_node_id)
      const foreignPublic = selectedServer?.metadata?.ip_address
      if (!foreignPublic) return

      const iranId = formData.iran_node_id || formData.node_id
      const iranNode = nodes.find(n => n.id === iranId)
      const grePeers = (iranNode?.metadata?.gre_peers || []) as Array<{
        remote?: string
        peer_inner?: string
        mtu?: number
        iface?: string
      }>
      const gre = grePeers.find(g => g.remote === foreignPublic && g.peer_inner)
      setFormData(prev => ({
        ...prev,
        remote_ip: gre?.peer_inner || foreignPublic
      }))
    }
  }, [formData.foreign_node_id, formData.iran_node_id, formData.node_id, formData.core, servers, nodes])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    e.stopPropagation()
    // Guard double-submit (double-click / Enter twice) before React state updates.
    if (submittingRef.current) {
      return
    }
    submittingRef.current = true
    setSubmitting(true)
    setFormError(null)
    try {
      let spec = getSpecForType(formData.core, formData.type)
      let tunnelType = formData.type
      
      spec.use_ipv6 = formData.use_ipv6 || false
      
      // Parse comma-separated ports
      const parsePorts = (portsStr: string): number[] => {
        return portsStr
          .split(',')
          .map(p => p.trim())
          .filter(p => p)
          .map(p => parseInt(p))
          .filter(p => !isNaN(p) && p > 0 && p <= 65535)
      }
      
      const ports = parsePorts(formData.ports)
      if (ports.length === 0) {
        alert('Please enter at least one valid port')
        submittingRef.current = false
        setSubmitting(false)
        return
      }
      
      if (formData.core === 'gost' && (formData.type === 'tcp' || formData.type === 'udp' || formData.type === 'grpc' || formData.type === 'tcpmux')) {
        const remoteIp = formData.remote_ip || (formData.use_ipv6 ? '::1' : '127.0.0.1')
        // For GOST, ports are equal (listen_port = forward_to port)
        spec.remote_ip = remoteIp
        spec.ports = ports  // Store multiple ports
        spec.listen_port = ports[0]  // Keep first port for backward compatibility
        spec.remote_port = ports[0]  // Keep first port for backward compatibility
      }
      
      if (formData.core === 'rathole') {
        const remoteHost = window.location.hostname
        const remotePort = formData.rathole_remote_addr || '23333'
        spec.remote_addr = `${remoteHost}:${remotePort}`
        if (formData.rathole_token) {
          spec.token = formData.rathole_token
        }
        spec.ports = ports
        spec.remote_port = ports[0]
        spec.listen_port = ports[0]
      }
      
      if (formData.core === 'chisel') {
        // For Chisel, ports are equal (reverse_port = local_port)
        spec.ports = ports  // Store multiple ports
        const firstPort = ports[0]
        spec.listen_port = firstPort
        spec.remote_port = firstPort
        spec.server_port = firstPort
        const controlPort = formData.chisel_control_port 
          ? parseInt(formData.chisel_control_port.toString())
          : firstPort + 10000
        spec.control_port = controlPort
        const panelHost = typeof window !== 'undefined' ? window.location.hostname : 'localhost'
        spec.panel_host = panelHost
      }

      if (formData.core === 'wstunnel') {
        spec.ports = ports
        const firstPort = ports[0]
        spec.listen_port = firstPort
        spec.remote_port = firstPort
        const controlPort = formData.wstunnel_control_port
          ? parseInt(formData.wstunnel_control_port.toString())
          : firstPort + 10000
        spec.control_port = controlPort
        spec.type = formData.type === 'udp' ? 'udp' : 'tcp'
        spec.local_addr = `127.0.0.1:${firstPort}`
        tunnelType = formData.type === 'udp' ? 'udp' : 'tcp'
      }

      if (formData.core === 'bore') {
        spec.ports = ports
        const firstPort = ports[0]
        spec.listen_port = firstPort
        spec.remote_port = firstPort
        spec.control_port = 7835
        spec.type = 'tcp'
        spec.local_host = '127.0.0.1'
        tunnelType = 'tcp'
      }
      
      if (formData.core === 'backhaul') {
        if (!formData.node_id) {
          alert('Backhaul tunnels require a node')
          submittingRef.current = false
          setSubmitting(false)
          return
        }
        // CRITICAL: For Backhaul, the Ports field is in BackhaulForm, not in the main formData.ports
        // backhaulState.public_port contains the comma-separated ports from the Backhaul form
        // We should use backhaulState.public_port, NOT formData.ports (which is for other cores)
        console.log('Backhaul tunnel creation - formData.ports:', formData.ports, 'type:', typeof formData.ports)
        console.log('Backhaul tunnel creation - backhaulState.public_port:', backhaulState.public_port)
        
        // Use backhaulState.public_port (from BackhaulForm) - it has the correct comma-separated ports
        // Only fallback to formData.ports if backhaulState.public_port is empty
        const portsToUse = backhaulState.public_port && backhaulState.public_port.trim() 
          ? backhaulState.public_port 
          : (formData.ports || '8080')
        
        const updatedBackhaulState = {
          ...backhaulState,
          public_port: portsToUse,
          target_port: portsToUse
        }
        console.log('Backhaul tunnel creation - updatedBackhaulState.public_port (final):', updatedBackhaulState.public_port)
        spec = buildBackhaulSpec(updatedBackhaulState, backhaulAdvanced)
        spec.use_ipv6 = formData.use_ipv6 || false
        // buildBackhaulSpec should already build ports array from public_port (formData.ports)
        // Verify ports were built correctly - if not, build them from parsed ports
        if (!spec.ports || !Array.isArray(spec.ports) || spec.ports.length === 0) {
          // buildBackhaulSpec didn't build ports, so build them from formData.ports
          if (backhaulAdvanced.customPorts && backhaulAdvanced.customPorts.trim()) {
            // Use customPorts if provided
            spec.ports = backhaulAdvanced.customPorts
              .split(/\r?\n/)
              .map((line) => line.trim())
              .filter(Boolean)
          } else if (ports.length > 0) {
            // Build from parsed ports (numbers) - format: "port=targetHost:port"
            const targetHost = spec.target_host || '127.0.0.1'
            const listenIp = spec.listen_ip || updatedBackhaulState.listen_ip || '0.0.0.0'
            spec.ports = ports.map(p => {
              // Format: "port=targetHost:port" or "listenIp:port=targetHost:port" if listenIp is set
              const listenPart = listenIp !== '0.0.0.0' ? `${listenIp}:${p}` : `${p}`
              return `${listenPart}=${targetHost}:${p}`
            })
          }
        }
        // Ensure ports array is properly formatted and has all ports
        if (spec.ports && Array.isArray(spec.ports) && spec.ports.length > 0) {
          console.log('Backhaul tunnel creation - final ports:', spec.ports, 'count:', spec.ports.length)
        } else {
          console.warn('Backhaul tunnel creation - no ports found! formData.ports:', formData.ports, 'publicPorts:', updatedBackhaulState.public_port)
        }
        tunnelType = backhaulState.transport
      }
      
      if (formData.core === 'frp') {
        if (!formData.node_id) {
          alert('FRP tunnels require a node')
          submittingRef.current = false
          setSubmitting(false)
          return
        }
        const bindPort = parseInt(formData.frp_bind_port) || 7000
        spec.bind_port = bindPort
        spec.ports = ports
        spec.listen_port = ports[0]
        spec.remote_port = ports[0]
        if (formData.frp_token) {
          spec.token = formData.frp_token
        }
        spec.local_ip = formData.frp_local_ip || '127.0.0.1'
        spec.local_port = ports[0]
        spec.type = formData.type === 'udp' ? 'udp' : 'tcp'
        tunnelType = formData.type === 'udp' ? 'udp' : 'tcp'
      }
      
      const payload = {
        name: formData.name,
        core: formData.core,
        type: tunnelType,
        node_id: formData.node_id || formData.iran_node_id || null,
        foreign_node_id: formData.foreign_node_id || null,
        iran_node_id: formData.iran_node_id || formData.node_id || null,
        spec: spec,
      }
      await api.post('/tunnels', payload)
      onSuccess()
    } catch (error: any) {
      console.error('Failed to create tunnel:', error)
      const message = apiErrorMessage(error, 'Failed to create tunnel')
      setFormError(message)
      submittingRef.current = false
      setSubmitting(false)
    }
  }

  const getSpecForType = (core: string, type: string): Record<string, any> => {
    const baseSpec: Record<string, any> = {}

    if (core === 'rathole') {
      return { ...baseSpec, remote_addr: '', token: '', local_addr: '127.0.0.1:8080' }
    }

    switch (type) {
      case 'grpc':
        return { ...baseSpec, service_name: 'GrpcService', uuid: generateUUID() }
      case 'udp':
        return { ...baseSpec, uuid: generateUUID(), header_type: 'none' }
      default:
        return baseSpec
    }
  }

  const handleCoreChange = (core: string) => {
    let newType = formData.type
    if (core === 'rathole' || core === 'chisel') {
      newType = core
    } else if (core === 'bore') {
      newType = 'tcp'
    } else if (core === 'frp' || core === 'wstunnel') {
      // Keep current type if it's tcp or udp, otherwise default to tcp
      newType = (formData.type === 'tcp' || formData.type === 'udp') ? formData.type : 'tcp'
    } else if (core === 'backhaul') {
      newType = backhaulState.transport
    } else if (formData.type === 'rathole' || formData.type === 'chisel' || formData.core === 'backhaul') {
      newType = 'tcp'
    }
    setFormData({ ...formData, core, type: newType })
  }

  const generateUUID = () => {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
      const r = (Math.random() * 16) | 0
      const v = c === 'x' ? r : (r & 0x3) | 0x8
      return v.toString(16)
    })
  }

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-[100] overflow-y-auto p-3 sm:p-4">
      <div className="bg-white dark:bg-gray-800 rounded-lg p-4 sm:p-6 w-full max-w-xl my-auto max-h-[90vh] overflow-y-auto">
        <h2 className="text-xl font-bold text-gray-900 dark:text-white mb-4">{t.tunnels.createTunnel}</h2>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Name
            </label>
            <input
              type="text"
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
              required
            />
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t.tunnels.iranNode}
              </label>
              <select
                value={formData.iran_node_id || formData.node_id}
                onChange={(e) => setFormData({ ...formData, iran_node_id: e.target.value, node_id: e.target.value })}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                required={formData.core === 'rathole' || formData.core === 'backhaul' || formData.core === 'frp' || formData.core === 'chisel' || formData.core === 'wstunnel' || formData.core === 'bore'}
              >
                <option value="">{t.tunnels.selectIranNode}</option>
                {nodes.map((node) => (
                  <option key={node.id} value={node.id}>
                    {node.name}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t.tunnels.foreignServer}
              </label>
              <select
                value={formData.foreign_node_id}
                onChange={(e) => setFormData({ ...formData, foreign_node_id: e.target.value })}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                required={formData.core === 'rathole' || formData.core === 'backhaul' || formData.core === 'frp' || formData.core === 'chisel' || formData.core === 'wstunnel' || formData.core === 'bore'}
              >
                <option value="">{t.tunnels.selectForeignServer}</option>
                {servers.map((server) => (
                  <option key={server.id} value={server.id}>
                    {server.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t.tunnels.core}
              </label>
              <select
                value={formData.core}
                onChange={(e) => handleCoreChange(e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
              >
                <option value="gost">GOST</option>
                <option value="rathole">Rathole</option>
                <option value="backhaul">Backhaul</option>
                <option value="chisel">Chisel</option>
                <option value="frp">FRP</option>
                <option value="wstunnel">Wstunnel</option>
                <option value="bore">Bore</option>
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                {t.tunnels.type}
              </label>
              <select
                value={formData.type}
                onChange={(e) => {
                  const value = e.target.value as BackhaulTransport
                  setFormData({ ...formData, type: value })
                  if (formData.core === 'backhaul') {
                    setBackhaulState((prev) => ({ ...prev, transport: value }))
                  }
                }}
                className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                disabled={formData.core === 'chisel' || formData.core === 'bore'}
              >
                {formData.core === 'chisel' ? (
                  <option value={formData.core}>{formData.core.charAt(0).toUpperCase() + formData.core.slice(1)}</option>
                ) : formData.core === 'bore' ? (
                  <option value="tcp">TCP</option>
                ) : formData.core === 'rathole' ? (
                  <>
                    <option value="tcp">TCP</option>
                    <option value="ws">WebSocket (WS)</option>
                  </>
                ) : formData.core === 'frp' || formData.core === 'wstunnel' ? (
                  <>
                    <option value="tcp">TCP</option>
                    <option value="udp">UDP</option>
                  </>
                ) : formData.core === 'backhaul' ? (
                  <>
                    <option value="tcp">TCP</option>
                    <option value="udp">UDP</option>
                    <option value="ws">WebSocket (WS)</option>
                    <option value="wsmux">WebSocket Mux</option>
                    <option value="tcpmux">TCPMux</option>
                  </>
                ) : (
                  <>
                    <option value="tcp">TCP</option>
                    <option value="udp">UDP</option>
                    <option value="grpc">gRPC</option>
                    <option value="tcpmux">TCPMux</option>
                  </>
                )}
              </select>
            </div>
          </div>

          {formData.core === 'gost' && (formData.type === 'tcp' || formData.type === 'udp' || formData.type === 'grpc' || formData.type === 'tcpmux') && (
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  {t.tunnels.remoteIP}
                </label>
                <input
                  type="text"
                  value={formData.remote_ip}
                  onChange={(e) =>
                    setFormData({ ...formData, remote_ip: e.target.value || '127.0.0.1' })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder="127.0.0.1 or [2001:db8::1]"
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Target on foreign node. If GRE exists between Iran and Foreign, peer inner IP is preferred automatically (MSS/MTU optimized).
                </p>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  {t.tunnels.ports}
                </label>
                <input
                  type="text"
                  value={formData.ports}
                  onChange={(e) =>
                    setFormData({ ...formData, ports: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder="8080,8081,8082"
                  required
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  {t.tunnels.portsDescription}
                </p>
              </div>
            </div>
          )}
          
          {formData.core === 'backhaul' && (
            <BackhaulForm
              state={backhaulState}
              onChange={(partial) => {
                setBackhaulState((prev) => ({ ...prev, ...partial }))
                if (partial.transport) {
                  setFormData((prev) => ({ ...prev, type: partial.transport as string }))
                }
              }}
              onOpenAdvanced={() => setShowBackhaulAdvanced(true)}
              acceptUdpVisible={
                backhaulState.transport === 'tcp' || backhaulState.transport === 'tcpmux'
              }
            />
          )}
          
          {formData.core === 'rathole' && (
            <>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Ports
                </label>
                <input
                  type="text"
                  value={formData.ports}
                  onChange={(e) =>
                    setFormData({ ...formData, ports: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder="8080,8081,8082"
                  required
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Ports (comma-separated, same for panel and node local service)
                </p>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Rathole Port
                </label>
                <input
                  type="number"
                  value={formData.rathole_remote_addr}
                  onChange={(e) =>
                    setFormData({ ...formData, rathole_remote_addr: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder="23333"
                  min="1"
                  max="65535"
                  required
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Rathole server port on panel (IP: {window.location.hostname})</p>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Token (Optional - Auto-generated if empty)
                </label>
                <input
                  type="text"
                  value={formData.rathole_token}
                  onChange={(e) =>
                    setFormData({ ...formData, rathole_token: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder="Leave empty for auto-generation"
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Authentication token (will be auto-generated if not provided)</p>
              </div>
            </div>
            </>
          )}
          
          {formData.core === 'chisel' && (
            <>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Ports
                </label>
                <input
                  type="text"
                  value={formData.ports}
                  onChange={(e) =>
                    setFormData({ ...formData, ports: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder="8080,8081,8082"
                  required
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Ports (comma-separated, same for reverse port and local port)
                </p>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Control Port
                </label>
                <input
                  type="number"
                  value={formData.chisel_control_port}
                  onChange={(e) =>
                    setFormData({ ...formData, chisel_control_port: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder={`${(parseInt(formData.ports.split(',')[0]?.trim()) || 8080) + 10000} (auto)`}
                  min="1"
                  max="65535"
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Chisel server control port (leave empty for auto: first port + 10000)
                </p>
              </div>
            </>
          )}

          {formData.core === 'wstunnel' && (
            <>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Ports
                </label>
                <input
                  type="text"
                  value={formData.ports}
                  onChange={(e) =>
                    setFormData({ ...formData, ports: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder="8080,8081,8082"
                  required
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Public ports on Iran node (forwarded to same ports on foreign)
                </p>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Control Port
                </label>
                <input
                  type="number"
                  value={formData.wstunnel_control_port}
                  onChange={(e) =>
                    setFormData({ ...formData, wstunnel_control_port: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder={`${(parseInt(formData.ports.split(',')[0]?.trim()) || 8080) + 10000} (auto)`}
                  min="1"
                  max="65535"
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Wstunnel WebSocket control port on Iran (leave empty for auto: first port + 10000)
                </p>
              </div>
            </>
          )}

          {formData.core === 'bore' && (
            <>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Ports
                </label>
                <input
                  type="text"
                  value={formData.ports}
                  onChange={(e) =>
                    setFormData({ ...formData, ports: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder="8080,8081,8082"
                  required
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Public TCP ports on Iran (forwarded to same ports on foreign)
                </p>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Control Port
                </label>
                <input
                  type="number"
                  value={7835}
                  disabled
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white opacity-70"
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Bore control port is fixed at 7835. Multiple bore tunnels on one Iran node share one server/secret.
                </p>
              </div>
            </>
          )}
          
          {formData.core === 'frp' && (
            <>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Bind Port
                  </label>
                  <input
                    type="number"
                    value={formData.frp_bind_port}
                    onChange={(e) =>
                      setFormData({ ...formData, frp_bind_port: e.target.value })
                    }
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                    placeholder="7000"
                    min="1"
                    max="65535"
                    required
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    FRP server port on panel (default: 7000)
                  </p>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Ports
                  </label>
                  <input
                    type="text"
                    value={formData.ports}
                    onChange={(e) =>
                      setFormData({ ...formData, ports: e.target.value })
                    }
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                    placeholder="8080,8081,8082"
                    required
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    Ports (comma-separated, same for remote port and local port)
                  </p>
                </div>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Token (Optional - Auto-generated if empty)
                </label>
                <input
                  type="text"
                  value={formData.frp_token}
                  onChange={(e) =>
                    setFormData({ ...formData, frp_token: e.target.value })
                  }
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                  placeholder="Leave empty for auto-generation"
                />
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Authentication token (will be auto-generated if not provided)</p>
              </div>
            </>
          )}
          
          {/* v4 to v6 tunnel checkbox - only for Rathole, Backhaul, Chisel, FRP (not GOST) */}
          {formData.core !== 'gost' && (
            <>
              <div className="flex items-center gap-2">
                <input
                  type="checkbox"
                  id="v4_to_v6"
                  checked={formData.use_ipv6}
                  onChange={(e) => setFormData({ ...formData, use_ipv6: e.target.checked })}
                  className="w-4 h-4 text-blue-600 bg-gray-100 border-gray-300 rounded focus:ring-blue-500 dark:focus:ring-blue-600 dark:ring-offset-gray-800 focus:ring-2 dark:bg-gray-700 dark:border-gray-600"
                />
                <label htmlFor="v4_to_v6" className="text-sm font-medium text-gray-700 dark:text-gray-300">
                  v4 to v6 tunnel
                </label>
              </div>
              <p className="text-xs text-gray-500 dark:text-gray-400 -mt-2">
                Enable this to create a tunnel from IPv4 (iran node) to IPv6 (node/target). Iran node listens on IPv4, target uses IPv6.
              </p>
            </>
          )}

          {formError && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300 whitespace-pre-wrap">
              {formError}
            </div>
          )}
          <div className="flex flex-col-reverse sm:flex-row gap-2 sm:gap-3 justify-end">
            <button
              type="button"
              onClick={onClose}
              disabled={submitting}
              className="px-4 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-600 disabled:opacity-50"
            >
              {t.tunnels.cancel}
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {submitting ? 'Creating…' : t.tunnels.createTunnel}
            </button>
          </div>
        </form>
        <BackhaulAdvancedDrawer
          open={showBackhaulAdvanced}
          state={backhaulAdvanced}
          onClose={() => setShowBackhaulAdvanced(false)}
          onChange={setBackhaulAdvanced}
        />
      </div>
    </div>
  )
}

const BACKHAUL_TRANSPORTS: BackhaulTransport[] = ['tcp', 'udp', 'ws', 'wsmux', 'tcpmux']

function BackhaulForm({
  state,
  onChange,
  onOpenAdvanced,
  acceptUdpVisible,
}: {
  state: BackhaulFormState
  onChange: (partial: Partial<BackhaulFormState>) => void
  onOpenAdvanced: () => void
  acceptUdpVisible?: boolean
}) {
  return (
    <div className="space-y-4">
      <div>
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
          Control Port
        </label>
        <input
          type="number"
          value={state.control_port}
          onChange={(e) => onChange({ control_port: e.target.value })}
          className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
          placeholder="3080"
          min={1}
          max={65535}
        />
        <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
          Port where the node connects back to the panel.
        </p>
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
          Ports
        </label>
        <input
          type="text"
          value={state.public_port}
          onChange={(e) => {
            onChange({ public_port: e.target.value, target_port: e.target.value })
          }}
          className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
          placeholder="8080,8081,8082"
        />
        <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
          Ports (comma-separated, same for public port and target port)
        </p>
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
          Token (Optional - Auto-generated if empty)
        </label>
        <input
          type="text"
          value={state.token}
          onChange={(e) => onChange({ token: e.target.value })}
          className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
          placeholder="Leave empty for auto-generation"
        />
        <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">Authentication token (will be auto-generated if not provided)</p>
      </div>

      {acceptUdpVisible && (
        <div className="flex items-center justify-between">
          <label className="text-sm font-medium text-gray-700 dark:text-gray-300">
            Allow UDP over TCP
          </label>
          <input
            type="checkbox"
            checked={state.accept_udp}
            onChange={() => onChange({ accept_udp: !state.accept_udp })}
            className="h-4 w-4 text-blue-600 rounded border-gray-300 dark:border-gray-600 focus:ring-blue-500"
          />
        </div>
      )}

      <div className="pt-2">
        <button
          type="button"
          onClick={onOpenAdvanced}
          className="px-3 py-2 text-sm font-medium text-blue-600 dark:text-blue-400 hover:underline"
        >
          Advanced settings
        </button>
      </div>
    </div>
  )
}

function BackhaulAdvancedDrawer({
  open,
  onClose,
  state,
  onChange,
}: {
  open: boolean
  onClose: () => void
  state: BackhaulAdvancedState
  onChange: (next: BackhaulAdvancedState) => void
}) {
  if (!open) {
    return null
  }

  const updateServer = (key: keyof BackhaulAdvancedServerState, value: string | boolean) => {
    onChange({
      ...state,
      server: {
        ...state.server,
        [key]: value,
      },
    })
  }

  const updateClient = (key: keyof BackhaulAdvancedClientState, value: string | boolean) => {
    onChange({
      ...state,
      client: {
        ...state.client,
        [key]: value,
      },
    })
  }

  return (
    <div className="fixed inset-0 z-[100] flex">
      <div className="flex-1 bg-black bg-opacity-40" onClick={onClose} />
      <div className="w-full sm:max-w-xl h-full bg-white dark:bg-gray-900 shadow-xl overflow-y-auto p-4 sm:p-6">
        <div className="flex justify-between items-center mb-6">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-white">Backhaul Advanced Settings</h3>
          <button
            onClick={onClose}
            className="text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
          >
            Close
          </button>
        </div>

        <div className="space-y-6">
          <div>
            <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 uppercase tracking-wide mb-3">
              Server Options
            </h4>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Keepalive (s)</label>
                <input
                  type="number"
                  value={state.server.keepalive_period}
                  onChange={(e) => updateServer('keepalive_period', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white"
                  min={1}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Heartbeat (s)</label>
                <input
                  type="number"
                  value={state.server.heartbeat}
                  onChange={(e) => updateServer('heartbeat', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white"
                  min={1}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Channel Size</label>
                <input
                  type="number"
                  value={state.server.channel_size}
                  onChange={(e) => updateServer('channel_size', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white"
                  min={1}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Mux Concurrency</label>
                <input
                  type="number"
                  value={state.server.mux_con}
                  onChange={(e) => updateServer('mux_con', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white"
                  min={1}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Log Level</label>
                <select
                  value={state.server.log_level}
                  onChange={(e) => updateServer('log_level', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white"
                >
                  <option value="panic">panic</option>
                  <option value="fatal">fatal</option>
                  <option value="error">error</option>
                  <option value="warn">warn</option>
                  <option value="info">info</option>
                  <option value="debug">debug</option>
                  <option value="trace">trace</option>
                </select>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Web UI Port</label>
                <input
                  type="number"
                  value={state.server.web_port}
                  onChange={(e) => updateServer('web_port', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white"
                  placeholder="0 (disable)"
                  min={0}
                />
              </div>
              <div className="col-span-2 flex items-center gap-3">
                <label className="text-sm font-medium text-gray-700 dark:text-gray-300 flex-1">Enable Sniffer</label>
                <input
                  type="checkbox"
                  checked={state.server.sniffer}
                  onChange={() => updateServer('sniffer', !state.server.sniffer)}
                  className="h-4 w-4 text-blue-600 rounded border-gray-300 dark:border-gray-600 focus:ring-blue-500"
                />
              </div>
              <div className="col-span-2">
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Sniffer Log Path</label>
                <input
                  type="text"
                  value={state.server.sniffer_log}
                  onChange={(e) => updateServer('sniffer_log', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white"
                  placeholder="/var/log/backhaul.json"
                />
              </div>
              <div className="col-span-2">
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">TLS Certificate Path</label>
                <input
                  type="text"
                  value={state.server.tls_cert}
                  onChange={(e) => updateServer('tls_cert', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white"
                />
              </div>
              <div className="col-span-2">
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">TLS Key Path</label>
                <input
                  type="text"
                  value={state.server.tls_key}
                  onChange={(e) => updateServer('tls_key', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white"
                />
              </div>
              <div className="col-span-2 flex items-center gap-3">
                <label className="text-sm font-medium text-gray-700 dark:text-gray-300 flex-1">Disable Optimizations</label>
                <input
                  type="checkbox"
                  checked={state.server.skip_optz}
                  onChange={() => updateServer('skip_optz', !state.server.skip_optz)}
                  className="h-4 w-4 text-blue-600 rounded border-gray-300 dark:border-gray-600 focus:ring-blue-500"
                />
              </div>
              <div className="col-span-2 flex items-center gap-3">
                <label className="text-sm font-medium text-gray-700 dark:text-gray-300 flex-1">Enable Proxy Protocol</label>
                <input
                  type="checkbox"
                  checked={state.server.proxy_protocol}
                  onChange={() => updateServer('proxy_protocol', !state.server.proxy_protocol)}
                  className="h-4 w-4 text-blue-600 rounded border-gray-300 dark:border-gray-600 focus:ring-blue-500"
                />
              </div>
              <div className="col-span-2 flex items-center gap-3">
                <label className="text-sm font-medium text-gray-700 dark:text-gray-300 flex-1">TCP Nodelay</label>
                <input
                  type="checkbox"
                  checked={state.server.nodelay}
                  onChange={() => updateServer('nodelay', !state.server.nodelay)}
                  className="h-4 w-4 text-blue-600 rounded border-gray-300 dark:border-gray-600 focus:ring-blue-500"
                />
              </div>
            </div>
          </div>

          <div>
            <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 uppercase tracking-wide mb-3">
              Client Options
            </h4>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Connection Pool</label>
                <input
                  type="number"
                  value={state.client.connection_pool}
                  onChange={(e) => updateClient('connection_pool', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white"
                  min={1}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Retry Interval (s)</label>
                <input
                  type="number"
                  value={state.client.retry_interval}
                  onChange={(e) => updateClient('retry_interval', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white"
                  min={1}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Dial Timeout (s)</label>
                <input
                  type="number"
                  value={state.client.dial_timeout}
                  onChange={(e) => updateClient('dial_timeout', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white"
                  min={1}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Keepalive (s)</label>
                <input
                  type="number"
                  value={state.client.keepalive_period}
                  onChange={(e) => updateClient('keepalive_period', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white"
                  min={1}
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Log Level</label>
                <select
                  value={state.client.log_level}
                  onChange={(e) => updateClient('log_level', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white"
                >
                  <option value="panic">panic</option>
                  <option value="fatal">fatal</option>
                  <option value="error">error</option>
                  <option value="warn">warn</option>
                  <option value="info">info</option>
                  <option value="debug">debug</option>
                  <option value="trace">trace</option>
                </select>
              </div>
              <div className="col-span-2">
                <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Edge IP (for WS/WSS)</label>
                <input
                  type="text"
                  value={state.client.edge_ip}
                  onChange={(e) => updateClient('edge_ip', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white"
                  placeholder="Optional CDN edge IP"
                />
              </div>
              <div className="col-span-2 flex items-center gap-3">
                <label className="text-sm font-medium text-gray-700 dark:text-gray-300 flex-1">Aggressive Pool</label>
                <input
                  type="checkbox"
                  checked={state.client.aggressive_pool}
                  onChange={() => updateClient('aggressive_pool', !state.client.aggressive_pool)}
                  className="h-4 w-4 text-blue-600 rounded border-gray-300 dark:border-gray-600 focus:ring-blue-500"
                />
              </div>
              <div className="col-span-2 flex items-center gap-3">
                <label className="text-sm font-medium text-gray-700 dark:text-gray-300 flex-1">TCP Nodelay</label>
                <input
                  type="checkbox"
                  checked={state.client.nodelay}
                  onChange={() => updateClient('nodelay', !state.client.nodelay)}
                  className="h-4 w-4 text-blue-600 rounded border-gray-300 dark:border-gray-600 focus:ring-blue-500"
                />
              </div>
              <div className="col-span-2 flex items-center gap-3">
                <label className="text-sm font-medium text-gray-700 dark:text-gray-300 flex-1">Disable Optimizations</label>
                <input
                  type="checkbox"
                  checked={state.client.skip_optz}
                  onChange={() => updateClient('skip_optz', !state.client.skip_optz)}
                  className="h-4 w-4 text-blue-600 rounded border-gray-300 dark:border-gray-600 focus:ring-blue-500"
                />
              </div>
            </div>
          </div>

          <div>
            <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 uppercase tracking-wide mb-3">
              Custom Ports
            </h4>
            <textarea
              value={state.customPorts}
              onChange={(e) => onChange({ ...state, customPorts: e.target.value })}
              className="w-full min-h-[120px] px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-800 dark:text-white"
              placeholder={`One entry per line. Examples:\n443\n443=127.0.0.1:8080\n443=[2001:db8::1]:8080\n2000-2100=127.0.0.1:22`}
            />
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              Format matches Backhaul ports syntax. Leave empty to use the single public port above.
            </p>
          </div>
        </div>
      </div>
    </div>
  )
}

function buildBackhaulSpec(
  base: BackhaulFormState,
  advanced: BackhaulAdvancedState,
  transportOverride?: BackhaulTransport,
): Record<string, any> {
  const transport = transportOverride ?? base.transport
  const controlPort = parseInt(base.control_port, 10)
  const publicPort = parseInt(base.public_port, 10)
  const targetPort = parseInt(base.target_port, 10)
  const listenIp = base.listen_ip.trim() || '0.0.0.0'
  const targetHost = base.target_host.trim() || '127.0.0.1'
  const token = base.token.trim()
  const panelHost = base.public_host.trim() || (typeof window !== 'undefined' ? window.location.hostname : '') || '127.0.0.1'

  const effectiveControlPort = !Number.isNaN(controlPort) && controlPort > 0
    ? controlPort
    : (!Number.isNaN(publicPort) && publicPort > 0
        ? publicPort
        : (!Number.isNaN(targetPort) && targetPort > 0 ? targetPort : 3080))
  
  // Parse comma-separated ports from public_port
  const parsePortsFromString = (portStr: string): number[] => {
    if (!portStr || typeof portStr !== 'string') {
      console.warn('parsePortsFromString: invalid input:', portStr, 'type:', typeof portStr)
      return []
    }
    const parsed = portStr
      .split(',')
      .map(p => p.trim())
      .filter(p => p)
      .map(p => parseInt(p, 10))
      .filter(p => !isNaN(p) && p > 0 && p <= 65535)
    console.log('parsePortsFromString: input:', portStr, '-> parsed:', parsed, 'count:', parsed.length)
    return parsed
  }
  
  // CRITICAL: Ensure base.public_port is a string before parsing
  const publicPortStr = String(base.public_port || '')
  console.log('buildBackhaulSpec: base.public_port (raw):', base.public_port, 'type:', typeof base.public_port, '-> string:', publicPortStr)
  const publicPorts = parsePortsFromString(publicPortStr)
  console.log('buildBackhaulSpec: parsed publicPorts:', publicPorts, 'count:', publicPorts.length)
  const effectivePublicPort = publicPorts.length > 0 ? publicPorts[0] : (!Number.isNaN(publicPort) && publicPort > 0 ? publicPort : effectiveControlPort)
  const effectiveTargetPort = publicPorts.length > 0 ? publicPorts[0] : (!Number.isNaN(targetPort) && targetPort > 0 ? targetPort : effectivePublicPort)

  const remoteAddr = base.remote_addr.trim() || `${panelHost}:${effectiveControlPort}`
  const listenedPort = listenIp !== '0.0.0.0' ? `${listenIp}:${effectivePublicPort}` : `${effectivePublicPort}`
  const defaultPortEntry = `${listenedPort}=${targetHost}:${effectiveTargetPort}`

  // Use customPorts if provided, otherwise build from comma-separated public_port
  let ports: string[] = []
  
  // CRITICAL: Check if customPorts is set AND has content
  // If customPorts is empty or just whitespace, use publicPorts instead
  const hasCustomPorts = advanced.customPorts && advanced.customPorts.trim().length > 0
  
  if (hasCustomPorts) {
    // User manually entered ports in CUSTOM PORTS field
    ports = advanced.customPorts
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
    console.log('buildBackhaulSpec: Using customPorts, count:', ports.length, 'ports:', ports)
  } else if (publicPorts.length > 0) {
    // Build ports array from comma-separated public_port (e.g., "8080,8081,8082")
    // This is the automatic conversion from Ports field to Backhaul format
    ports = publicPorts.map(p => {
      const listenedPort = listenIp !== '0.0.0.0' ? `${listenIp}:${p}` : `${p}`
      return `${listenedPort}=${targetHost}:${p}`
    })
    console.log('buildBackhaulSpec: Built ports from publicPorts:', publicPorts, '-> ports:', ports, 'count:', ports.length)
  }
  
  if (ports.length === 0) {
    ports.push(defaultPortEntry)
    console.log('buildBackhaulSpec: No ports found, using default:', defaultPortEntry)
  }
  
  // Final verification - ensure we have ports
  console.log('buildBackhaulSpec: Final ports array:', ports, 'count:', ports.length)

  const serverOptions: Record<string, any> = {}
  Object.entries(advanced.server).forEach(([key, value]) => {
    if (booleanServerKeys.has(key)) {
      if (value) {
        serverOptions[key] = true
      }
      return
    }
    if (numericServerKeys.has(key)) {
      const num = Number(value)
      if (!Number.isNaN(num) && value !== '') {
        serverOptions[key] = num
      }
      return
    }
    if (stringServerKeys.has(key)) {
      const val = typeof value === 'string' ? value.trim() : value
      if (val) {
        serverOptions[key] = val
      }
    }
  })

  const clientOptions: Record<string, any> = {}
  Object.entries(advanced.client).forEach(([key, value]) => {
    if (booleanClientKeys.has(key)) {
      if (value) {
        clientOptions[key] = true
      }
      return
    }
    if (numericClientKeys.has(key)) {
      const num = Number(value)
      if (!Number.isNaN(num) && value !== '') {
        clientOptions[key] = num
      }
      return
    }
    if (stringClientKeys.has(key)) {
      const val = typeof value === 'string' ? value.trim() : value
      if (val) {
        clientOptions[key] = val
      }
    }
  })

  const spec: Record<string, any> = {
    transport,
    bind_addr: `0.0.0.0:${effectiveControlPort}`,
    remote_addr: remoteAddr,
    listen_ip: listenIp,
    control_port: effectiveControlPort,
    public_port: effectivePublicPort,
    listen_port: effectivePublicPort,
    target_host: targetHost,
    target_port: effectiveTargetPort,
    target_addr: `${targetHost}:${effectiveTargetPort}`,
    public_host: panelHost,
    ports,
  }

  if (token) {
    spec.token = token
  }
  if (base.accept_udp && (transport === 'tcp' || transport === 'tcpmux')) {
    spec.accept_udp = true
  }
  if (Object.keys(serverOptions).length > 0) {
    spec.server_options = serverOptions
  }
  if (Object.keys(clientOptions).length > 0) {
    spec.client_options = clientOptions
  }

  return spec
}

function parseBackhaulSpec(spec: Record<string, any>, currentType: string): {
  state: BackhaulFormState
  advanced: BackhaulAdvancedState
} {
  const state = createDefaultBackhaulState()
  const advanced = createDefaultBackhaulAdvancedState()

  if (BACKHAUL_TRANSPORTS.includes(currentType as BackhaulTransport)) {
    state.transport = currentType as BackhaulTransport
  }

  if (!spec) {
    return { state, advanced }
  }

  const controlPortCandidate =
    spec.control_port ??
    extractPort(spec.bind_addr) ??
    extractPort(spec.remote_addr)
  if (controlPortCandidate) {
    state.control_port = String(controlPortCandidate)
  }

  state.listen_ip = spec.listen_ip ?? state.listen_ip

  const publicPortCandidate =
    spec.public_port ??
    spec.listen_port ??
    derivePortFromPorts(spec.ports)
  if (publicPortCandidate) {
    state.public_port = String(publicPortCandidate)
  }

  if (spec.target_host) {
    state.target_host = String(spec.target_host)
  } else if (typeof spec.target_addr === 'string') {
    const parsed = parseAddressPort(spec.target_addr)
    state.target_host = parsed.host
  }

  const targetPortCandidate =
    spec.target_port ??
    (typeof spec.target_addr === 'string'
      ? parseAddressPort(spec.target_addr).port
      : undefined)
  if (targetPortCandidate) {
    state.target_port = String(targetPortCandidate)
  }

  state.token = spec.token ?? ''
  state.public_host = spec.public_host ?? ''
  state.remote_addr = spec.remote_addr ?? ''
  state.accept_udp = Boolean(spec.accept_udp)

  if (Array.isArray(spec.ports) && spec.ports.length > 0) {
    advanced.customPorts = spec.ports.join('\n')
  }

  const serverOptions = spec.server_options || {}
  Object.entries(advanced.server).forEach(([key, defaultValue]) => {
    const value = serverOptions[key]
    if (value === undefined || value === null) {
      return
    }
    if (typeof defaultValue === 'boolean') {
      advanced.server[key as keyof BackhaulAdvancedServerState] = Boolean(value)
    } else {
      advanced.server[key as keyof BackhaulAdvancedServerState] = String(value)
    }
  })

  const clientOptions = spec.client_options || {}
  Object.entries(advanced.client).forEach(([key, defaultValue]) => {
    const value = clientOptions[key]
    if (value === undefined || value === null) {
      return
    }
    if (typeof defaultValue === 'boolean') {
      advanced.client[key as keyof BackhaulAdvancedClientState] = Boolean(value)
    } else {
      advanced.client[key as keyof BackhaulAdvancedClientState] = String(value)
    }
  })

  return { state, advanced }
}

function extractPort(value: unknown): string | undefined {
  if (typeof value === 'number') {
    return value.toString()
  }
  if (typeof value === 'string') {
    const parts = value.split(':')
    const port = parts[parts.length - 1]
    if (port && !Number.isNaN(Number(port))) {
      return port
    }
  }
  return undefined
}

function derivePortFromPorts(value: unknown): string | undefined {
  if (!Array.isArray(value) || value.length === 0) {
    return undefined
  }
  const first = value[0]
  if (typeof first !== 'string') {
    return undefined
  }
  const [left] = first.split('=')
  if (!left) {
    return undefined
  }
  const segments = left.split(':')
  const port = segments[segments.length - 1]
  return port && !Number.isNaN(Number(port)) ? port : undefined
}

export default Tunnels
