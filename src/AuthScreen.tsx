import { useEffect, useState, type FormEvent } from 'react'
import { supabase } from './lib/supabase'

function authMessage(cause: unknown): string {
  const error = cause as { code?: string; status?: number; message?: string } | null
  if (error?.code === 'over_email_send_rate_limit' ||
      (error?.status === 429 && /email|sending/i.test(error.message || ''))) {
    return 'Supabase has reached its email sending limit. If your account is already confirmed, sign in with your password. Otherwise, wait before requesting another email.'
  }
  if (error?.code === 'over_request_rate_limit' || error?.status === 429) {
    return 'Too many sign-in attempts. Please wait a few minutes before trying again.'
  }
  return cause instanceof Error ? cause.message : 'Authentication failed. Please try again.'
}

type AuthMode = 'signIn' | 'signUp' | 'reset' | 'link'

type Props = {
  loading: boolean
  recovering: boolean
  onRecoveryComplete: () => void
}

export default function AuthScreen({ loading, recovering, onRecoveryComplete }: Props) {
  const [mode, setMode] = useState<AuthMode>('signIn')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [googleAvailable, setGoogleAvailable] = useState<boolean | null>(null)

  useEffect(() => {
    const url = import.meta.env.VITE_SUPABASE_URL
    const key = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY
    if (!url || !key) return
    let active = true
    fetch(`${url.replace(/\/$/, '')}/auth/v1/settings`, { headers: { apikey: key } })
      .then(response => response.ok ? response.json() : null)
      .then(settings => { if (active) setGoogleAvailable(settings?.external?.google === true) })
      .catch(() => { if (active) setGoogleAvailable(false) })
    return () => { active = false }
  }, [])

  function switchMode(next: AuthMode) {
    setMode(next)
    setPassword('')
    setConfirmPassword('')
    setError('')
    setNotice('')
  }

  async function submitEmail(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!supabase) return
    if ((mode === 'signUp' || recovering) && password !== confirmPassword) {
      setError('Passwords do not match.')
      return
    }
    setBusy(true)
    setError('')
    setNotice('')
    try {
      if (recovering) {
        const { error: authError } = await supabase.auth.updateUser({ password })
        if (authError) throw authError
        onRecoveryComplete()
      } else if (mode === 'signIn') {
        const { error: authError } = await supabase.auth.signInWithPassword({ email: email.trim(), password })
        if (authError) throw authError
      } else if (mode === 'signUp') {
        const { data, error: authError } = await supabase.auth.signUp({
          email: email.trim(), password,
          options: { emailRedirectTo: window.location.origin },
        })
        if (authError) throw authError
        if (!data.session) setNotice('Check your email to confirm your account, then sign in.')
      } else if (mode === 'reset') {
        const { error: authError } = await supabase.auth.resetPasswordForEmail(email.trim(), { redirectTo: window.location.origin })
        if (authError) throw authError
        setNotice('If this account exists, a password reset link is on its way.')
      } else {
        const { error: authError } = await supabase.auth.signInWithOtp({ email: email.trim(), options: { emailRedirectTo: window.location.origin } })
        if (authError) throw authError
        setNotice('Check your email for a sign-in link.')
      }
    } catch (cause) {
      setError(authMessage(cause))
    } finally {
      setBusy(false)
    }
  }

  async function signInWithGoogle() {
    if (!supabase || !googleAvailable) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const { error: authError } = await supabase.auth.signInWithOAuth({
        provider: 'google', options: { redirectTo: window.location.origin },
      })
      if (authError) throw authError
    } catch (cause) {
      setError(authMessage(cause))
      setBusy(false)
    }
  }

  const disabled = !supabase || loading || busy
  const title = recovering ? 'Choose a new password' : mode === 'signUp' ? 'Create your account' : mode === 'reset' ? 'Reset your password' : 'Sign in to continue'
  const description = recovering ? 'Enter a new password for your account.' : mode === 'signUp' ? 'Create an account to save your own datasets.' : mode === 'reset' ? 'We’ll email you a link to reset your password.' : mode === 'link' ? 'We’ll email you a secure sign-in link.' : 'Choose Google or use your email and password.'
  const submitLabel = recovering ? 'Save new password' : mode === 'signUp' ? 'Create account' : mode === 'reset' ? 'Send reset link' : mode === 'link' ? 'Email me a sign-in link' : 'Sign in with email'

  return <main className="auth-screen"><section className="auth-panel" aria-labelledby="auth-title">
    <div className="brand"><div className="brand-mark"><span/><span/><span/><span/></div><div><strong>HYPERSION</strong><small>GEN PLATFORM</small></div></div>
    <div className="auth-copy"><span>HYPERSION WORKSPACE</span><h1 id="auth-title">{title}</h1><p>{description}</p></div>
    {!recovering && mode === 'signIn' && <>
      <button className="auth-google" type="button" disabled={disabled || googleAvailable !== true} onClick={() => void signInWithGoogle()}><span className="google-mark" aria-hidden="true">G</span>Continue with Google</button>
      {googleAvailable === false && <p className="auth-provider-note">Google sign-in is awaiting setup. Use email below.</p>}
      <div className="auth-divider"><span>or</span></div>
    </>}
    <form onSubmit={event => void submitEmail(event)}>
      {!recovering && <><label className="auth-label" htmlFor="auth-email">Email address</label><input id="auth-email" className="auth-input" type="email" autoComplete="email" required value={email} onChange={event => setEmail(event.target.value)}/></>}
      {(recovering || mode === 'signIn' || mode === 'signUp') && <><label className="auth-label auth-password-label" htmlFor="auth-password">{recovering ? 'New password' : 'Password'}</label><input id="auth-password" className="auth-input" type="password" autoComplete={mode === 'signIn' && !recovering ? 'current-password' : 'new-password'} minLength={6} required value={password} onChange={event => setPassword(event.target.value)}/></>}
      {(recovering || mode === 'signUp') && <><label className="auth-label auth-password-label" htmlFor="auth-confirm">Confirm password</label><input id="auth-confirm" className="auth-input" type="password" autoComplete="new-password" minLength={6} required value={confirmPassword} onChange={event => setConfirmPassword(event.target.value)}/></>}
      <button className="auth-submit" type="submit" disabled={disabled}>{busy ? 'Please wait…' : loading ? 'Checking session…' : submitLabel}</button>
    </form>
    {!recovering && <div className="auth-actions">
      {mode === 'signIn' ? <><button type="button" onClick={() => switchMode('reset')}>Forgot password?</button><button type="button" onClick={() => switchMode('signUp')}>Create account</button><button type="button" onClick={() => switchMode('link')}>Use an email link instead</button></> : <button type="button" onClick={() => switchMode('signIn')}>Back to sign in</button>}
    </div>}
    {!supabase && <p className="auth-message" role="alert">Supabase is not configured for this app.</p>}
    {error && <p className="auth-message" role="alert">{error}</p>}
    {notice && <p className="auth-message success" role="status">{notice}</p>}
    <p className="auth-foot">Protected by Supabase authentication</p>
  </section></main>
}
