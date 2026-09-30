import { getApp, getApps, initializeApp } from 'firebase/app'
import { getAuth } from 'firebase/auth'

const firebaseConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY || 'AIzaSyBbF7_AGnmSk11ciBtQxsF9byta-FXvCcc',
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN || 'hypersion-50897.firebaseapp.com',
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID || 'hypersion-50897',
  appId: import.meta.env.VITE_FIREBASE_APP_ID || '1:512250899679:web:b1ef62f43a81918bb04d6a',
}

const firebaseApp = getApps().length ? getApp() : initializeApp(firebaseConfig)

export const firebaseAuth = getAuth(firebaseApp)
