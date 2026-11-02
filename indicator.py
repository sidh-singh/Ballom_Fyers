import pandas as pd
import numpy as np


class Indicator:
    """Smoothed Heiken Ashi v3 Indicator"""
    
    def __init__(self):
        """Initialize SHA Indicator"""
        pass

    # ─── TradingView-compatible exponential MA core ───
    @staticmethod
    def _tv_exp_ma(series, length, alpha):
        """
        TradingView-compatible exponential MA.
        Seeds with SMA of the first `length` non-NaN values, then
        applies the standard exponential recursion.  This matches
        ta.ema() (alpha=2/(len+1)) and ta.rma() (alpha=1/len) in Pine.
        """
        values = series.values.astype(float)
        n = len(values)
        result = np.full(n, np.nan)

        # Find first window of `length` consecutive non-NaN values
        run = 0
        seed_end = -1
        for i in range(n):
            if not np.isnan(values[i]):
                run += 1
                if run >= length:
                    seed_end = i
                    break
            else:
                run = 0

        if seed_end == -1:
            # Not enough data — return NaN series
            return pd.Series(result, index=series.index)

        # SMA seed
        seed_start = seed_end - length + 1
        result[seed_end] = np.mean(values[seed_start:seed_end + 1])

        # Exponential recursion
        for i in range(seed_end + 1, n):
            v = values[i]
            if np.isnan(v):
                result[i] = result[i - 1]
            else:
                result[i] = alpha * v + (1.0 - alpha) * result[i - 1]

        return pd.Series(result, index=series.index)

    @staticmethod
    def _tv_exp_ma_first_seed(series, length, alpha):
        """
        Exponential MA that seeds with the first non-NaN value (Pine SMMA style).
        Unlike _tv_exp_ma which seeds with SMA, this seeds immediately.
        Matches Pine:  smma := na(smma[1]) ? src : (smma[1]*(length-1) + src) / length
        """
        values = series.values.astype(float)
        n = len(values)
        result = np.full(n, np.nan)

        # Find first non-NaN value
        first_valid = -1
        for i in range(n):
            if not np.isnan(values[i]):
                first_valid = i
                break

        if first_valid == -1:
            return pd.Series(result, index=series.index)

        result[first_valid] = values[first_valid]
        for i in range(first_valid + 1, n):
            v = values[i]
            if np.isnan(v):
                result[i] = result[i - 1]
            else:
                result[i] = alpha * v + (1.0 - alpha) * result[i - 1]

        return pd.Series(result, index=series.index)

    def calculate_sha_v3(self, df, length=10, ma_type='EMA'):
        """
        Calculate Smoothed Heiken Ashi v3
        
        Args:
            df: DataFrame with OHLCV data
            length: Smoothing length (used for both pre and post HA smoothing)
            ma_type: MA type (used for both pre and post HA smoothing)
        
        Returns:
            DataFrame with SHA OHLC columns
        """
        df = df.copy()

        # Step 1: Pre-smooth the OHLC
        o = self._ma(df['Open'], length, ma_type, df.get('Volume'))
        h = self._ma(df['High'], length, ma_type, df.get('Volume'))
        l = self._ma(df['Low'], length, ma_type, df.get('Volume'))
        c = self._ma(df['Close'], length, ma_type, df.get('Volume'))

        # Step 2: Heiken Ashi Calculation
        ha_close = (o + h + l + c) / 4

        # Find first bar where ALL pre-smoothed values are valid.
        # With TV-compatible MAs the first (length-1) bars are NaN.
        valid_mask = o.notna() & h.notna() & l.notna() & c.notna()
        ha_open = pd.Series(np.nan, index=df.index, dtype=float)

        if valid_mask.any():
            first_valid_pos = int(valid_mask.values.argmax())  # first True
            ha_open.iloc[first_valid_pos] = (
                o.iloc[first_valid_pos] + c.iloc[first_valid_pos]
            ) / 2

            # Recursive calculation from first valid bar onward
            for i in range(first_valid_pos + 1, len(df)):
                ha_open.iloc[i] = (ha_open.iloc[i - 1] + ha_close.iloc[i - 1]) / 2

        ha_high = pd.concat([h, ha_open, ha_close], axis=1).max(axis=1)
        ha_low = pd.concat([l, ha_open, ha_close], axis=1).min(axis=1)

        # Step 3: Smooth again after HA (same length & MA type)
        sha_open = self._ma(ha_open, length, ma_type, df.get('Volume'))
        sha_high = self._ma(ha_high, length, ma_type, df.get('Volume'))
        sha_low = self._ma(ha_low, length, ma_type, df.get('Volume'))
        sha_close = self._ma(ha_close, length, ma_type, df.get('Volume'))

        sha = pd.DataFrame({
            'Open': sha_open,
            'High': sha_high,
            'Low': sha_low,
            'Close': sha_close
        }, index=df.index)

        return sha
    
    def calculate_rsi(self, series, length=14, ma_type='RMA'):
        """
        Calculate RSI (Relative Strength Index).

        Matches TradingView's ta.rsi() when ma_type='RMA'.

        Args:
            series: pd.Series of close prices
            length: RSI period (default 14)
            ma_type: MA type used to smooth gains/losses (default 'RMA')

        Returns:
            pd.Series: RSI values (0–100)
        """
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)

        avg_gain = self._ma(gain, length, ma_type)
        avg_loss = self._ma(loss, length, ma_type)

        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
        # Where avg_loss is 0, RSI should be 100
        rsi = rsi.fillna(100.0)
        return rsi

    def calculate_adx(self, high, low, close, length=14):
        """
        Calculate ADX, +DI, -DI, ATR and True Range (Wilder / TradingView ta.adx).

        Matches TradingView's ta.adx()/ta.dmi() when smoothed with RMA. ATR is an
        intermediate value of the DI computation and is returned for free (reused
        by the regime spike guard, so no separate ATR pass is needed). Pure /
        vectorized; reuses the existing Wilder RMA in _ma().

        Args:
            high, low, close: pd.Series of prices (shared/aligned index)
            length: ADX/ATR period (default 14)

        Returns:
            pd.DataFrame with columns: TR, ATR, plus_DI, minus_DI, ADX
        """
        high = high.astype(float)
        low = low.astype(float)
        close = close.astype(float)

        prev_close = close.shift(1)
        # True range = max(H-L, |H-Cprev|, |L-Cprev|); first bar falls back to H-L
        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)

        # Directional movement
        up_move = high.diff()          # high - prev_high
        down_move = -low.diff()        # prev_low - low
        plus_dm = pd.Series(
            np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
            index=high.index,
        )
        minus_dm = pd.Series(
            np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
            index=high.index,
        )

        # Wilder-smoothed (RMA) — same smoothing TradingView uses for ADX
        atr = self._ma(tr, length, 'RMA')
        safe_atr = atr.replace(0, np.nan)              # avoid divide-by-zero
        plus_di = 100.0 * self._ma(plus_dm, length, 'RMA') / safe_atr
        minus_di = 100.0 * self._ma(minus_dm, length, 'RMA') / safe_atr

        di_sum = (plus_di + minus_di).replace(0, np.nan)
        dx = 100.0 * (plus_di - minus_di).abs() / di_sum
        adx = self._ma(dx, length, 'RMA')

        return pd.DataFrame({
            'TR': tr,
            'ATR': atr,
            'plus_DI': plus_di,
            'minus_DI': minus_di,
            'ADX': adx,
        }, index=high.index)

    def _ma(self, series, length, ma_type='EMA', volume=None):
        """
        Calculate moving average
        
        Args:
            series: Price series
            length: MA length
            ma_type: Type of MA (SMA, EMA, WMA, RMA, VWMA, DEMA, TEMA, ZLEMA, HMA, ALMA, SMMA, LSMA, DONCHIAN)
            volume: Volume series (required for VWMA)
        
        Returns:
            pd.Series with MA values
        """
        if length <= 0:
            return series
    
        ma_type = ma_type.upper()
        
        if ma_type == 'SMA':
            return series.rolling(window=length).mean()
        
        elif ma_type == 'EMA':
            # TradingView ta.ema(): SMA-seeded, alpha = 2/(length+1)
            return self._tv_exp_ma(series, length, alpha=2.0 / (length + 1))
        
        elif ma_type == 'WMA':
            weights = np.arange(1, length + 1)
            return series.rolling(window=length).apply(
                lambda x: np.dot(x, weights) / weights.sum(), raw=True
            )
        
        elif ma_type == 'RMA':
            # TradingView ta.rma(): SMA-seeded, alpha = 1/length
            return self._tv_exp_ma(series, length, alpha=1.0 / length)
        
        elif ma_type == 'VWMA':
            if volume is None:
                return self._tv_exp_ma(series, length, alpha=2.0 / (length + 1))
            pv = series * volume
            return pv.rolling(window=length).sum() / volume.rolling(window=length).sum()
        
        elif ma_type == 'DEMA':
            ema1 = self._tv_exp_ma(series, length, alpha=2.0 / (length + 1))
            ema2 = self._tv_exp_ma(ema1, length, alpha=2.0 / (length + 1))
            return 2 * ema1 - ema2
        
        elif ma_type == 'TEMA':
            ema1 = self._tv_exp_ma(series, length, alpha=2.0 / (length + 1))
            ema2 = self._tv_exp_ma(ema1, length, alpha=2.0 / (length + 1))
            ema3 = self._tv_exp_ma(ema2, length, alpha=2.0 / (length + 1))
            return 3 * ema1 - 3 * ema2 + ema3
        
        elif ma_type == 'ZLEMA':
            lag = (length - 1) // 2
            zlema_series = 2 * series - series.shift(lag)
            return self._tv_exp_ma(zlema_series, length, alpha=2.0 / (length + 1))
        
        elif ma_type == 'HMA':
            half_length = length // 2
            sqrt_length = int(np.sqrt(length))
            wma_half = series.rolling(window=half_length).apply(
                lambda x: np.dot(x, np.arange(1, half_length + 1)) / np.arange(1, half_length + 1).sum(), 
                raw=True
            )
            wma_full = series.rolling(window=length).apply(
                lambda x: np.dot(x, np.arange(1, length + 1)) / np.arange(1, length + 1).sum(), 
                raw=True
            )
            diff = 2 * wma_half - wma_full
            weights_sqrt = np.arange(1, sqrt_length + 1)
            return diff.rolling(window=sqrt_length).apply(
                lambda x: np.dot(x, weights_sqrt) / weights_sqrt.sum(), raw=True
            )
        
        elif ma_type == 'ALMA':
            offset = 0.85
            sigma = 6
            m = offset * (length - 1)
            s = length / sigma
            weights = np.exp(-((np.arange(length) - m) ** 2) / (2 * s * s))
            weights /= weights.sum()
            return series.rolling(window=length).apply(
                lambda x: np.dot(x, weights), raw=True
            )
        
        elif ma_type == 'SMMA':
            # Pine SMMA: seeds with first value (NOT SMA), then
            # smma = (smma[1] * (length-1) + src) / length  (alpha = 1/length)
            return self._tv_exp_ma_first_seed(series, length, alpha=1.0 / length)
        
        elif ma_type == 'SWMA':
            # SWMA in Pine is ta.swma() which is a 4-bar symmetric weighted avg
            # Approximate with WMA(4) for short series; length param is ignored
            w = np.array([1, 2, 2, 1], dtype=float)
            return series.rolling(window=4).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)
        
        elif ma_type == 'LSMA':
            return series.rolling(window=length).apply(
                lambda x: np.polyfit(np.arange(len(x)), x, 1)[0] * (len(x)-1) + np.polyfit(np.arange(len(x)), x, 1)[1],
                raw=True
            )
        
        elif ma_type == 'DONCHIAN':
            return (series.rolling(window=length).max() + series.rolling(window=length).min()) / 2
        
        else:
            # Unknown MA type → fall back to TV-compatible EMA
            return self._tv_exp_ma(series, length, alpha=2.0 / (length + 1))
