import { useState, useEffect } from 'react'
import api from '../api/client'
import { apiErrorMessage } from '../api/errors'
import { useLanguage } from '../contexts/LanguageContext'

interface FrpSettings {
  enabled: boolean
  port: number
  token?: string
}

interface TelegramSettings {
  enabled: boolean
  bot_token?: string
  admin_ids: string[]
  backup_enabled?: boolean
  backup_interval?: number
  backup_interval_unit?: string
}

interface TunnelSettings {
  auto_reapply_enabled?: boolean
  auto_reapply_interval?: number
  auto_reapply_interval_unit?: string
}

interface SettingsData {
  frp: FrpSettings
  telegram: TelegramSettings
  tunnel?: TunnelSettings
}

const Settings = () => {
  const { t } = useLanguage()
  const [settings, setSettings] = useState<SettingsData>({
    frp: { enabled: false, port: 7000 },
    telegram: { enabled: false, admin_ids: [] },
    tunnel: { auto_reapply_enabled: false, auto_reapply_interval: 60, auto_reapply_interval_unit: 'minutes' }
  })
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState<{ type: 'success' | 'error', text: string } | null>(null)

  useEffect(() => {
    loadSettings()
  }, [])

  const loadSettings = async () => {
    try {
      const response = await api.get('/settings')
      setSettings(response.data)
    } catch (error) {
      console.error('Failed to load settings:', error)
      setMessage({ type: 'error', text: apiErrorMessage(error, t.settings.failedToLoad) })
    } finally {
      setLoading(false)
    }
  }

  const saveSettings = async () => {
    setSaving(true)
    setMessage(null)
    try {
      await api.put('/settings', settings)
      setMessage({ type: 'success', text: t.settings.settingsSaved })
      await loadSettings()
    } catch (error) {
      console.error('Failed to save settings:', error)
      setMessage({ type: 'error', text: apiErrorMessage(error, t.settings.failedToSave) })
    } finally {
      setSaving(false)
    }
  }

  const updateFrp = (updates: Partial<FrpSettings>) => {
    setSettings(prev => ({
      ...prev,
      frp: { ...prev.frp, ...updates }
    }))
  }

  const updateTelegram = (updates: Partial<TelegramSettings>) => {
    setSettings(prev => ({
      ...prev,
      telegram: { ...prev.telegram, ...updates }
    }))
  }

  const updateTunnel = (updates: Partial<TunnelSettings>) => {
    setSettings(prev => ({
      ...prev,
      tunnel: { ...prev.tunnel, ...updates } as TunnelSettings
    }))
  }

  const addAdminId = () => {
    const newId = prompt(t.settings.enterAdminId)
    if (newId && newId.trim()) {
      updateTelegram({
        admin_ids: [...settings.telegram.admin_ids, newId.trim()]
      })
    }
  }

  const removeAdminId = (index: number) => {
    updateTelegram({
      admin_ids: settings.telegram.admin_ids.filter((_, i) => i !== index)
    })
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="text-gray-600 dark:text-gray-400">{t.settings.loadingSettings}</div>
      </div>
    )
  }

  return (
    <div>
      <h1 className="text-3xl font-bold text-gray-900 dark:text-white mb-8">{t.settings.title}</h1>
      
      {message && (
        <div className={`mb-4 p-4 rounded-lg ${
          message.type === 'success' 
            ? 'bg-green-100 dark:bg-green-900 text-green-800 dark:text-green-200' 
            : 'bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-200'
        }`}>
          {message.text}
        </div>
      )}

      <div className="space-y-6">
        {/* FRP Communication Settings */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-4">{t.settings.frpCommunication}</h2>
          <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
            {t.settings.frpDescription}
          </p>
          
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <label htmlFor="frp-enabled" className="text-sm font-medium text-gray-700 dark:text-gray-300">
                {t.settings.enableFrp}
              </label>
              <button
                type="button"
                id="frp-enabled"
                onClick={() => updateFrp({ enabled: !settings.frp.enabled })}
                className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 ${
                  settings.frp.enabled ? 'bg-blue-600' : 'bg-gray-300 dark:bg-gray-600'
                }`}
              >
                <span
                  className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                    settings.frp.enabled ? 'translate-x-6' : 'translate-x-1'
                  }`}
                />
              </button>
            </div>

            {settings.frp.enabled && (
              <>
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    {t.settings.frpPort}
                  </label>
                  <input
                    type="number"
                    value={settings.frp.port}
                    onChange={(e) => updateFrp({ port: parseInt(e.target.value) || 7000 })}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                    placeholder="7000"
                    min="1"
                    max="65535"
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    {t.settings.frpPortDescription}
                  </p>
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    {t.settings.frpTokenOptional}
                  </label>
                  <input
                    type="text"
                    value={settings.frp.token || ''}
                    onChange={(e) => updateFrp({ token: e.target.value || undefined })}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                    placeholder="Leave empty for no authentication"
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    {t.settings.frpTokenDescription}
                  </p>
                </div>
              </>
            )}
          </div>
        </div>

        {/* Telegram Bot Settings */}
        <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-4">{t.settings.telegramBot}</h2>
          <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
            {t.settings.telegramDescription}
          </p>
          
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <label htmlFor="telegram-enabled" className="text-sm font-medium text-gray-700 dark:text-gray-300">
                {t.settings.enableTelegram}
              </label>
              <button
                type="button"
                id="telegram-enabled"
                onClick={() => updateTelegram({ enabled: !settings.telegram.enabled })}
                className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 ${
                  settings.telegram.enabled ? 'bg-blue-600' : 'bg-gray-300 dark:bg-gray-600'
                }`}
              >
                <span
                  className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                    settings.telegram.enabled ? 'translate-x-6' : 'translate-x-1'
                  }`}
                />
              </button>
            </div>

            {settings.telegram.enabled && (
              <>
                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    {t.settings.botToken}
                  </label>
                  <input
                    type="password"
                    value={settings.telegram.bot_token || ''}
                    onChange={(e) => updateTelegram({ bot_token: e.target.value || undefined })}
                    className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                    placeholder="Enter bot token from @BotFather"
                  />
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    {t.settings.botTokenDescription}
                  </p>
                </div>

                <div>
                  <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    {t.settings.adminUserIds}
                  </label>
                  <div className="space-y-2">
                    {settings.telegram.admin_ids.map((id, index) => (
                      <div key={index} className="flex items-center gap-2">
                        <input
                          type="text"
                          value={id}
                          onChange={(e) => {
                            const newIds = [...settings.telegram.admin_ids]
                            newIds[index] = e.target.value
                            updateTelegram({ admin_ids: newIds })
                          }}
                          className="flex-1 px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                        />
                        <button
                          onClick={() => removeAdminId(index)}
                          className="px-3 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700"
                        >
                          {t.settings.remove}
                        </button>
                      </div>
                    ))}
                    <button
                      onClick={addAdminId}
                      className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700"
                    >
                      {t.settings.addAdminId}
                    </button>
                  </div>
                  <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                    {t.settings.adminUserIdsDescription}
                  </p>
                </div>

                <div className="border-t border-gray-200 dark:border-gray-700 pt-4 mt-4">
                  <h3 className="text-md font-semibold text-gray-900 dark:text-white mb-3">{t.settings.automaticBackup}</h3>
                  
                  <div className="flex items-center gap-2 mb-4">
                    <input
                      type="checkbox"
                      id="backup-enabled"
                      checked={settings.telegram.backup_enabled || false}
                      onChange={(e) => updateTelegram({ backup_enabled: e.target.checked })}
                      className="rounded"
                    />
                    <label htmlFor="backup-enabled" className="text-sm font-medium text-gray-700 dark:text-gray-300">
                      {t.settings.enableBackup}
                    </label>
                  </div>

                  {settings.telegram.backup_enabled && (
                    <div className="space-y-4">
                      <div className="flex items-center gap-4">
                        <div className="flex-1">
                          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                            {t.settings.backupInterval}
                          </label>
                          <input
                            type="number"
                            value={settings.telegram.backup_interval || 60}
                            onChange={(e) => updateTelegram({ backup_interval: parseInt(e.target.value) || 60 })}
                            className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                            placeholder="60"
                            min="1"
                          />
                        </div>
                        <div className="flex-1">
                          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                            {t.settings.intervalUnit}
                          </label>
                          <select
                            value={settings.telegram.backup_interval_unit || 'minutes'}
                            onChange={(e) => updateTelegram({ backup_interval_unit: e.target.value })}
                            className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                          >
                            <option value="minutes">{t.settings.minutes}</option>
                            <option value="hours">{t.settings.hours}</option>
                          </select>
                        </div>
                      </div>
                      <p className="text-xs text-gray-500 dark:text-gray-400">
                        {t.settings.backupDescription}
                      </p>
                    </div>
                  )}
                </div>
              </>
            )}
          </div>
        </div>

        {/* Tunnel Settings */}
        <div className="bg-white dark:bg-gray-800 rounded-xl shadow-sm border border-gray-200 dark:border-gray-700 p-6">
          <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-4">{t.settings.tunnelAutoReapply || 'Tunnel Auto Reapply'}</h2>
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <label className="text-sm font-medium text-gray-700 dark:text-gray-300">
                  {t.settings.enableTunnelAutoReapply || 'Enable Automatic Tunnel Reapply'}
                </label>
                <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  {t.settings.tunnelAutoReapplyDescription || 'Automatically reapply all tunnels at specified intervals'}
                </p>
              </div>
              <button
                type="button"
                onClick={() => updateTunnel({ auto_reapply_enabled: !(settings.tunnel?.auto_reapply_enabled || false) })}
                className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2 ${
                  settings.tunnel?.auto_reapply_enabled ? 'bg-blue-600' : 'bg-gray-300 dark:bg-gray-600'
                }`}
              >
                <span
                  className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                    settings.tunnel?.auto_reapply_enabled ? 'translate-x-6' : 'translate-x-1'
                  }`}
                />
              </button>
            </div>

            {settings.tunnel?.auto_reapply_enabled && (
              <div className="space-y-4 pl-4 border-l-2 border-gray-200 dark:border-gray-700">
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      {t.settings.tunnelReapplyInterval || 'Reapply Interval'}
                    </label>
                    <input
                      type="number"
                      value={settings.tunnel?.auto_reapply_interval || 60}
                      onChange={(e) => updateTunnel({ auto_reapply_interval: parseInt(e.target.value) || 60 })}
                      className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                      placeholder="60"
                      min="1"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      {t.settings.intervalUnit || 'Interval Unit'}
                    </label>
                    <select
                      value={settings.tunnel?.auto_reapply_interval_unit || 'minutes'}
                      onChange={(e) => updateTunnel({ auto_reapply_interval_unit: e.target.value })}
                      className="w-full px-3 py-2 border border-gray-300 dark:border-gray-600 rounded-lg dark:bg-gray-700 dark:text-white"
                    >
                      <option value="minutes">{t.settings.minutes}</option>
                      <option value="hours">{t.settings.hours}</option>
                    </select>
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>

        {/* Save Button */}
        <div className="flex justify-end">
          <button
            onClick={saveSettings}
            disabled={saving}
            className="px-6 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {saving ? t.settings.saving : t.settings.saveSettings}
          </button>
        </div>
      </div>
    </div>
  )
}

export default Settings
