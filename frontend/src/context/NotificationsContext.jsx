import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { useAuth } from './AuthContext';

const NotificationsContext = createContext({ count: 0, refresh: () => {} });

/**
 * Provides the global unread notification count for the notification center.
 *
 * The count is refreshed:
 *  - on mount
 *  - whenever the window regains focus (so switching back from another tab/page updates it)
 *  - every 30 seconds as a fallback
 *  - manually via the `refresh` function returned by the hook
 */
export function NotificationsProvider({ children }) {
  const [count, setCount] = useState(0);
  const timerRef = useRef(null);
  // The provider sits above the auth gate, so it also mounts on /login. Only
  // poll once a user is signed in — an unauthenticated GET 401s, and api.js
  // sends the browser to /login on a failed refresh, which reloads the login
  // page and remounts this provider in a loop.
  const { user } = useAuth();

  const refresh = useCallback(async () => {
    try {
      const data = await api.get('/notifications/unread-count');
      setCount(Number(data?.unread ?? 0));
    } catch {
      // ignore fetch errors – badge count is non-critical
    }
  }, []);

  // Polling fallback every 30 s.
  useEffect(() => {
    if (!user) {
      setCount(0);
      return;
    }
    refresh();
    timerRef.current = setInterval(refresh, 30_000);
    return () => clearInterval(timerRef.current);
  }, [refresh, user]);

  // Refresh when the window regains focus (e.g. user switches back from Notifications page).
  useEffect(() => {
    if (!user) return;
    const onFocus = () => refresh();
    window.addEventListener('focus', onFocus);
    return () => window.removeEventListener('focus', onFocus);
  }, [refresh, user]);

  return (
    <NotificationsContext.Provider value={{ count, refresh }}>
      {children}
    </NotificationsContext.Provider>
  );
}

export function useNotifications() {
  return useContext(NotificationsContext);
}
