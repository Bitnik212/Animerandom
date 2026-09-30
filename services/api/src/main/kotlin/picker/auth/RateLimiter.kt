package picker.auth

import io.lettuce.core.ExperimentalLettuceCoroutinesApi
import io.lettuce.core.ScriptOutputType
import picker.config.RateLimit
import picker.errors.Errors
import picker.infra.redis.Redis
import java.util.UUID

/**
 * Sliding-window limiter in Redis for the auth endpoints (`ratelimit:auth:*`). Each
 * attempt is a member of a sorted set scored by time; members older than the window drop
 * out. Over the limit → `429 too-many-attempts` with `Retry-After`.
 */
@OptIn(ExperimentalLettuceCoroutinesApi::class)
class RateLimiter(private val redis: Redis, private val now: () -> Long = System::currentTimeMillis) {
    suspend fun hit(key: String, limit: RateLimit) {
        val windowMs = limit.windowSeconds * 1000
        val result =
            redis.commands.eval<List<Long>>(
                SCRIPT,
                ScriptOutputType.MULTI,
                arrayOf(key),
                now().toString(),
                windowMs.toString(),
                limit.count.toString(),
                UUID.randomUUID().toString(),
            ) ?: return
        val (allowed, retryAfterMs) = result
        if (allowed == 0L) throw Errors.tooManyAttempts((retryAfterMs + 999) / 1000)
    }

    private companion object {
        // Returns {1, 0} when allowed, {0, ms until the oldest attempt leaves the window} otherwise.
        const val SCRIPT = """
            local now = tonumber(ARGV[1])
            local window = tonumber(ARGV[2])
            redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
            if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then
              local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
              return {0, tonumber(oldest[2]) + window - now}
            end
            redis.call('ZADD', KEYS[1], now, ARGV[4])
            redis.call('PEXPIRE', KEYS[1], window)
            return {1, 0}
        """
    }
}
