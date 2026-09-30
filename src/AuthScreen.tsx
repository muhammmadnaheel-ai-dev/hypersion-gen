import { useEffect, useState, type FormEvent } from 'react'
import {
  confirmPasswordReset,
  createUserWithEmailAndPassword,
  GoogleAuthProvider,
  isSignInWithEmailLink,
  sendPasswordResetEmail,
  sendSignInLinkToEmail,
  signInWithEmailAndPassword,
  signInWithEmailLink,
  signInWithPopup,
} from 'firebase/auth'
import { firebaseAuth } from './lib/firebase'

function authMessage(cause: unknown): string {
  const error = cause as { code?: string; status?: number; message?: string } | null
  if (error?.code === 'auth/too-many-requests' || error?.status === 429) {
    return 'Too many sign-in attempts. Please wait a few minutes before trying again.'
  }
  if (error?.code === 'auth/operation-not-allowed') {
    return 'This sign-in method is not enabled in Firebase Authentication.'
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

  useEffect(() => {
    if (!firebaseAuth || !isSignInWithEmailLink(firebaseAuth, window.location.href)) return
    const savedEmail = window.localStorage.getItem('firebaseSignInEmail')
    if (!savedEmail) {
      setMode('link')
      setNotice('Enter the email address where we sent your sign-in link.')
      return
    }
    signInWithEmailLink(firebaseAuth, savedEmail, window.location.href)
      .then(() => window.localStorage.removeItem('firebaseSignInEmail'))
      .catch(cause => setError(authMessage(cause)))
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
    if (!firebaseAuth) return
    if ((mode === 'signUp' || recovering) && password !== confirmPassword) {
      setError('Passwords do not match.')
      return
    }
    setBusy(true)
    setError('')
    setNotice('')
    try {
      if (recovering) {
        const code = new URLSearchParams(window.location.search).get('oobCode')
        if (!code) throw new Error('This password reset link is invalid or has expired.')
        await confirmPasswordReset(firebaseAuth, code, password)
        window.history.replaceState({}, '', window.location.pathname)
        onRecoveryComplete()
      } else if (mode === 'signIn') {
        await signInWithEmailAndPassword(firebaseAuth, email.trim(), password)
      } else if (mode === 'signUp') {
        await createUserWithEmailAndPassword(firebaseAuth, email.trim(), password)
      } else if (mode === 'reset') {
        await sendPasswordResetEmail(firebaseAuth, email.trim())
        setNotice('If this account exists, a password reset link is on its way.')
      } else {
        if (isSignInWithEmailLink(firebaseAuth, window.location.href)) {
          const savedEmail = window.localStorage.getItem('firebaseSignInEmail') || email.trim()
          await signInWithEmailLink(firebaseAuth, savedEmail, window.location.href)
          window.localStorage.removeItem('firebaseSignInEmail')
        } else {
          await sendSignInLinkToEmail(firebaseAuth, email.trim(), {
            url: window.location.origin,
            handleCodeInApp: true,
          })
          window.localStorage.setItem('firebaseSignInEmail', email.trim())
          setNotice('Check your email for a sign-in link.')
        }
      }
    } catch (cause) {
      setError(authMessage(cause))
    } finally {
      setBusy(false)
    }
  }

  async function signInWithGoogle() {
    if (!firebaseAuth) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await signInWithPopup(firebaseAuth, new GoogleAuthProvider())
    } catch (cause) {
      setError(authMessage(cause))
      setBusy(false)
    }
  }

  const disabled = !firebaseAuth || loading || busy
  const title = recovering ? 'Choose a new password' : mode === 'signUp' ? 'Create your account' : mode === 'reset' ? 'Reset your password' : 'Sign in to continue'
  const description = recovering ? 'Enter a new password for your account.' : mode === 'signUp' ? 'Create an account to save your own datasets.' : mode === 'reset' ? 'We’ll email you a link to reset your password.' : mode === 'link' ? 'We’ll email you a secure sign-in link.' : 'Choose Google or use your email and password.'
  const submitLabel = recovering ? 'Save new password' : mode === 'signUp' ? 'Create account' : mode === 'reset' ? 'Send reset link' : mode === 'link' ? 'Email me a sign-in link' : 'Sign in with email'

  return <main className="auth-screen"><section className="auth-panel" aria-labelledby="auth-title">
    <div className="brand"><div className="brand-mark"><span/><span/><span/><span/></div><div><strong>HYPERSION</strong><small>GEN PLATFORM</small></div></div>
    <div className="auth-copy"><span>HYPERSION WORKSPACE</span><h1 id="auth-title">{title}</h1><p>{description}</p></div>
    {!recovering && mode === 'signIn' && <>
      <button className="auth-google" type="button" disabled={disabled} onClick={() => void signInWithGoogle()}><span className="google-mark" aria-hidden="true">G</span>Continue with Google</button>
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
    {!firebaseAuth && <p className="auth-message" role="alert">Firebase Authentication is not configured for this app.</p>}
    {error && <p className="auth-message" role="alert">{error}</p>}
    {notice && <p className="auth-message success" role="status">{notice}</p>}
    <p className="auth-foot">Protected by Firebase Authentication</p>
  </section></main>
}
