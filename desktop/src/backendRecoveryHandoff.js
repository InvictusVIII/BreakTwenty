const RECOVERY_ID_RE = /^[0-9a-f-]{36}$/i;

function boundedMessage(value) {
  return String(value || '').trim().slice(0, 500) || null;
}

class BackendRecoveryHandoff {
  constructor({
    softTimeoutMs = 20_000,
    fallbackTimeoutMs = 30_000,
  } = {}) {
    this.softTimeoutMs = softTimeoutMs;
    this.fallbackTimeoutMs = fallbackTimeoutMs;
    this.pending = new Map();
  }

  acknowledge(request = {}) {
    const recoveryId = String(request.recoveryId || '');
    if (!RECOVERY_ID_RE.test(recoveryId)) {
      return { status: 'ignored', message: 'Recovery acknowledgement was invalid.' };
    }
    const pending = this.pending.get(recoveryId);
    if (!pending) {
      return { status: 'ignored', message: 'Recovery acknowledgement was no longer pending.' };
    }
    this.pending.delete(recoveryId);
    pending.resolve({
      ok: request.status === 'ok',
      message: boundedMessage(request.message),
      acknowledgedAt: new Date().toISOString(),
    });
    return { status: 'ok' };
  }

  waitForAcknowledgement(recoveryId) {
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        if (!this.pending.has(recoveryId)) return;
        this.pending.delete(recoveryId);
        resolve({ ok: false, timedOut: true, message: 'Renderer recovery acknowledgement timed out.' });
      }, Math.max(0, this.softTimeoutMs));
      this.pending.set(recoveryId, {
        resolve: (result) => {
          clearTimeout(timer);
          resolve(result);
        },
      });
    });
  }

  waitForDidFinishLoad(webContents) {
    return new Promise((resolve) => {
      let settled = false;
      const finish = (result) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        webContents.removeListener?.('did-finish-load', onLoad);
        resolve(result);
      };
      const onLoad = () => finish({
        ok: true,
        completion: 'fallback_reload',
        completedAt: new Date().toISOString(),
      });
      const timer = setTimeout(() => finish({
        ok: false,
        completion: 'fallback_timeout',
        message: 'Fallback renderer reload did not finish before its deadline.',
      }), Math.max(0, this.fallbackTimeoutMs));
      webContents.once('did-finish-load', onLoad);
      try {
        webContents.reloadIgnoringCache();
      } catch (error) {
        finish({
          ok: false,
          completion: 'fallback_failed',
          message: boundedMessage(error?.message) || 'Fallback renderer reload failed.',
        });
      }
    });
  }

  async run(webContents, { recoveryId, backendRecoveredAt } = {}) {
    if (!RECOVERY_ID_RE.test(String(recoveryId || ''))) {
      throw new Error('Backend recovery handoff requires a valid recovery ID.');
    }
    if (!webContents || webContents.isDestroyed?.()) {
      return {
        ok: false,
        completion: 'renderer_unavailable',
        message: 'The renderer was unavailable for backend recovery.',
      };
    }

    const acknowledgement = this.waitForAcknowledgement(recoveryId);
    try {
      webContents.send('breaktwenty:backend-recovered', {
        recoveryId,
        backendRecoveredAt,
      });
    } catch (error) {
      const pending = this.pending.get(recoveryId);
      this.pending.delete(recoveryId);
      pending?.resolve({ ok: false, message: boundedMessage(error?.message) });
    }
    const soft = await acknowledgement;
    if (soft.ok) {
      return {
        ...soft,
        completion: 'soft_acknowledged',
      };
    }
    if (webContents.isDestroyed?.()) {
      return {
        ...soft,
        ok: false,
        completion: 'renderer_unavailable',
      };
    }
    const fallback = await this.waitForDidFinishLoad(webContents);
    return {
      ...fallback,
      softFailure: {
        timedOut: Boolean(soft.timedOut),
        message: soft.message || null,
      },
    };
  }
}

async function completeBackendRecoveryHandoff({
  handoff,
  webContents,
  recovery,
  finalize,
}) {
  const rendererCompletion = await handoff.run(webContents, recovery);
  await finalize(rendererCompletion);
  return rendererCompletion;
}

module.exports = {
  BackendRecoveryHandoff,
  RECOVERY_ID_RE,
  completeBackendRecoveryHandoff,
};
