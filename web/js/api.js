/**
 * LISA Front-End REST API SDK
 * Production client for consuming backend endpoints with session credentials and error handling.
 */

class ApiError extends Error {
  constructor(message, status = 500, details = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.details = details;
  }
}

async function request(endpoint, options = {}) {
  const config = {
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
    credentials: 'include', // Automatically includes HTTP-only session cookies
    ...options,
  };

  if (config.body && typeof config.body === 'object') {
    config.body = JSON.stringify(config.body);
  }

  let res;
  try {
    res = await fetch(endpoint, config);
  } catch (netErr) {
    throw new ApiError('Network connection failed. Please check your internet or server connection.', 0, netErr);
  }

  let data = null;
  const contentType = res.headers.get('content-type') || '';
  if (contentType.includes('application/json')) {
    try {
      data = await res.json();
    } catch {
      data = null;
    }
  } else {
    data = await res.text();
  }

  if (!res.ok) {
    const errorMsg = (data && data.error) ? data.error : `HTTP Error ${res.status}: ${res.statusText}`;
    throw new ApiError(errorMsg, res.status, data);
  }

  return data;
}

export const api = {
  // Authentication & Sessions
  auth: {
    signup: (payload) => request('/api/auth/signup', { method: 'POST', body: payload }),
    signin: (payload) => request('/api/auth/signin', { method: 'POST', body: payload }),
    signout: () => request('/api/auth/signout', { method: 'POST' }),
    me: () => request('/api/auth/me', { method: 'GET' }),
    updateTier: (payload) => request('/api/auth/update-tier', { method: 'POST', body: payload }),
    linkTelegram: (payload) => request('/api/auth/link-telegram', { method: 'POST', body: payload }),
  },

  // Predictions & Ledger
  picks: {
    get: (params = {}) => {
      const qs = new URLSearchParams(params).toString();
      return request(`/api/picks${qs ? `?${qs}` : ''}`, { method: 'GET' });
    },
  },

  ledger: {
    get: () => request('/api/ledger', { method: 'GET' }),
  },

  // Verification
  verify: {
    status: (userId) => request(`/api/verify-status?user_id=${encodeURIComponent(userId)}`, { method: 'GET' }),
    token: (userId, token) => request('/api/verify-token', { method: 'POST', body: { user_id: userId, token } }),
  },

  // System Telemetry & Diagnostics
  status: {
    get: () => request('/api/status', { method: 'GET' }),
  },
};

export default api;
