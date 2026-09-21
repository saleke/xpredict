/**
 * LISA Authentication Controller
 * Manages user session state, reactive authentication events, and persistent credentials.
 */
import { api } from './api.js';

class AuthController {
  constructor() {
    this._user = null;
    this._initialized = false;
    this._listeners = new Set();
    this._loadCachedUser();
  }

  _loadCachedUser() {
    try {
      const raw = localStorage.getItem('lisa_user');
      if (raw) {
        this._user = JSON.parse(raw);
      }
    } catch {
      this._user = null;
    }
  }

  _persistUser(user) {
    this._user = user;
    try {
      if (user) {
        localStorage.setItem('lisa_user', JSON.stringify(user));
        localStorage.setItem('lisa_tier', user.tier || 'free');
        if (user.telegram_verified) {
          localStorage.setItem('lisa_telegram_unlocked', 'true');
        }
      } else {
        localStorage.removeItem('lisa_user');
      }
    } catch (e) {
      console.warn('Failed to persist user to localStorage:', e);
    }
    this._notifyListeners();
  }

  _notifyListeners() {
    for (const listener of this._listeners) {
      try {
        listener(this._user);
      } catch (err) {
        console.error('Auth state listener error:', err);
      }
    }
  }

  /**
   * Subscribe to authentication changes (login, logout, tier update).
   * Immediately calls the listener with current user state.
   */
  onAuthStateChanged(callback) {
    this._listeners.add(callback);
    callback(this._user);
    return () => this._listeners.delete(callback);
  }

  /**
   * Initialize and verify session against server truth.
   */
  async init() {
    if (this._initialized) return this._user;
    try {
      const data = await api.auth.me();
      if (data && data.authenticated && data.user) {
        this._persistUser(data.user);
      } else {
        this._persistUser(null);
      }
    } catch (err) {
      console.warn('Session verification check failed:', err);
      // Keep cached user if offline, or reset if 401
      if (err.status === 401) {
        this._persistUser(null);
      }
    } finally {
      this._initialized = true;
    }
    return this._user;
  }

  async signup(email, password, displayName = '', tier = 'free') {
    const data = await api.auth.signup({
      email,
      password,
      display_name: displayName,
      tier,
    });
    if (data && data.user) {
      this._persistUser(data.user);
      return data.user;
    }
    throw new Error('Invalid response from server.');
  }

  async signin(email, password) {
    const data = await api.auth.signin({ email, password });
    if (data && data.user) {
      this._persistUser(data.user);
      return data.user;
    }
    throw new Error('Invalid response from server.');
  }

  async signout() {
    try {
      await api.auth.signout();
    } catch (err) {
      console.warn('Signout API error:', err);
    } finally {
      this._persistUser(null);
      localStorage.removeItem('lisa_user');
      localStorage.removeItem('lisa_tier');
      localStorage.removeItem('lisa_telegram_unlocked');
    }
  }

  async linkTelegram(telegramId, telegramUsername = '') {
    const data = await api.auth.linkTelegram({
      telegram_id: telegramId,
      telegram_username: telegramUsername,
    });
    if (data && data.user) {
      this._persistUser(data.user);
      return data.user;
    }
    return null;
  }

  getUser() {
    return this._user;
  }

  isAuthenticated() {
    return this._user !== null;
  }

  getTier() {
    return this._user ? (this._user.tier || 'free') : 'free';
  }

  isTelegramVerified() {
    return this._user ? Boolean(this._user.telegram_verified) : false;
  }

  async updateTier(tier) {
    try {
      const res = await api.auth.updateTier({ tier });
      if (res && res.user) {
        this._user = res.user;
        this._notifyListeners();
        return res;
      }
    } catch (err) {
      console.warn('[auth] Failed to persist tier update to backend:', err);
    }
    return null;
  }
}

export const auth = new AuthController();
export default auth;
