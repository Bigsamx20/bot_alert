//@version=5
strategy("Scalping Bot - RSI + EMA + Spike + Confluence", overlay=true, 
         margin_long=100, margin_short=100, 
         default_qty_type=strategy.percent_of_equity, default_qty_value=10, 
         calc_on_every_tick=true, calc_on_order_fills=true)

// ---------------------- INPUTS ----------------------
rsiLength = input.int(14, "RSI Length")
rsiOverbought = input.int(97, "RSI Overbought")
rsiOversold = input.int(3, "RSI Oversold")
emaLength = input.int(50, "EMA Length")
emaDistancePercent = input.float(65, "EMA Distance %")
spikeMultiplier = input.float(12, "Spike Candle Multiplier")
confluenceToggle = input.bool(true, "Enable Confluence Alerts")
emailAlertToggle = input.bool(true, "Send Email Alerts")

// ---------------------- CALCULATIONS ----------------------
rsi = ta.rsi(close, rsiLength)
ema = ta.ema(close, emaLength)
candleBody = math.abs(close - open)
prevCandleBody = math.abs(close[1] - open[1])

// EMA distance in percent
emaDistancePercentCurrent = math.abs(close - ema) / ema * 100

// Spike candle detection
spikeCandle = candleBody >= spikeMultiplier * prevCandleBody

// Confluence detection
rsiOB = rsi >= rsiOverbought
rsiOS = rsi <= rsiOversold
emaFarAbove = close > ema and emaDistancePercentCurrent >= emaDistancePercent
emaFarBelow = close < ema and emaDistancePercentCurrent >= emaDistancePercent
confluenceCondition = confluenceToggle and ((rsiOB and emaFarAbove) or (rsiOS and emaFarBelow))

// ---------------------- ALERT FLAGS (ONE PER CANDLE) ----------------------
var bool rsiAlertFired = false
var bool emaAlertFired = false
var bool spikeAlertFired = false
var bool confluenceAlertFired = false

if ta.change(time("5"))
    // Reset per candle
    rsiAlertFired := false
    emaAlertFired := false
    spikeAlertFired := false
    confluenceAlertFired := false

// ---------------------- ALERT CONDITIONS ----------------------
if not rsiAlertFired
    if rsiOB
        strategy.alert("RSI Overbought (" + str.tostring(rsi) + ")", alert.freq_once_per_bar)
        rsiAlertFired := true
    if rsiOS
        strategy.alert("RSI Oversold (" + str.tostring(rsi) + ")", alert.freq_once_per_bar)
        rsiAlertFired := true

if not emaAlertFired
    if emaFarAbove
        strategy.alert("Price far ABOVE EMA (" + str.tostring(emaDistancePercentCurrent) + "%)", alert.freq_once_per_bar)
        emaAlertFired := true
    if emaFarBelow
        strategy.alert("Price far BELOW EMA (" + str.tostring(emaDistancePercentCurrent) + "%)", alert.freq_once_per_bar)
        emaAlertFired := true

if not spikeAlertFired
    if spikeCandle
        strategy.alert("Spike Candle Detected (x" + str.tostring(spikeMultiplier) + ")", alert.freq_once_per_bar)
        spikeAlertFired := true

if not confluenceAlertFired
    if confluenceCondition
        strategy.alert("CONFLUENCE ALERT: RSI + EMA", alert.freq_once_per_bar)
        confluenceAlertFired := true

// ---------------------- PLOT ----------------------
plot(ema, color=color.yellow, title="EMA")
hline(rsiOverbought, color=color.red, title="RSI Overbought")
hline(rsiOversold, color=color.green, title="RSI Oversold")
