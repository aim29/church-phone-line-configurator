// voice.js
//
// Deployed as a Twilio Function alongside the church's audio Assets.
// Reads its call-flow config from the CONFIG_JSON environment variable
// (set by the provisioning app) and returns TwiML.
//
// CONFIG_JSON shape, single-recording mode:
//   { "mode": "single", "welcome_path": "/welcome.mp3", "recording_path": "/recording.mp3" }
//
// CONFIG_JSON shape, menu mode:
//   {
//     "mode": "menu",
//     "welcome_path": "/welcome.mp3",
//     "options": {
//       "1": { "label": "the Sunday sermon", "path": "/option-1.mp3" },
//       "2": { "label": "this week's notices", "path": "/option-2.mp3" }
//     }
//   }

const MAX_ATTEMPTS = 3;

// Helper to build fully qualified public asset URLs
function getPublicAssetUrl(context, path) {
  if (!path) return null;
  const cleanPath = path.startsWith('/') ? path : `/${path}`;
  const url = `https://${context.DOMAIN_NAME}${cleanPath}`;
  console.log(`[Asset URL] Formatted: ${url}`);
  return url;
}

exports.handler = function (context, event, callback) {
  const twiml = new Twilio.twiml.VoiceResponse();

  console.log(`[Incoming Call] Sid: ${event.CallSid || 'Unknown'}, From: ${event.From || 'Unknown'}`);
  console.log(`[Domain]: ${context.DOMAIN_NAME}`);

  let config;
  try {
    config = JSON.parse(context.CONFIG_JSON);
    console.log(`[Config] Parsed. Mode: "${config.mode}"`);
  } catch (err) {
    console.error('[Config Error] Failed to parse CONFIG_JSON:', err.message);
    twiml.say("Sorry, this phone line has not been configured yet.");
    twiml.hangup();
    return callback(null, twiml);
  }

  const digit = event.Digits;
  const attempt = parseInt(event.attempt || "0", 10);
  const isFirstRequest = !digit && attempt === 0;

  console.log(`[Call State] Digit: ${digit || 'None'}, Attempt: ${attempt}, First Request: ${isFirstRequest}`);

  // Play welcome message on first hit
  if (isFirstRequest) {
    const welcomeUrl = getPublicAssetUrl(context, config.welcome_path);
    if (welcomeUrl) {
      twiml.play(welcomeUrl);
    }
  }

  if (config.mode === "single") {
    console.log('[Flow] Single-recording mode.');
    const recordingUrl = getPublicAssetUrl(context, config.recording_path);
    if (recordingUrl) {
      twiml.play(recordingUrl);
    } else {
      twiml.say("Sorry, no recording is available at the moment.");
    }
    twiml.hangup();
    return callback(null, twiml);
  }

  // --- Menu mode ---
  console.log('[Flow] Menu mode.');

  if (digit && config.options && config.options[digit]) {
    console.log(`[Menu] User selected key ${digit}`);
    const option = config.options[digit];
    const optionUrl = getPublicAssetUrl(context, option.path);
    if (optionUrl) {
      twiml.play(optionUrl);
    } else {
      twiml.say("Sorry, that recording is not available at the moment.");
    }
    twiml.hangup();
    return callback(null, twiml);
  }

  if (attempt >= MAX_ATTEMPTS) {
    console.warn(`[Menu] Max retry attempts (${MAX_ATTEMPTS}) reached.`);
    twiml.say("Sorry, no valid option was selected. Goodbye.");
    twiml.hangup();
    return callback(null, twiml);
  }

  const gather = twiml.gather({
    numDigits: 1,
    timeout: 6,
    action: `/voice?attempt=${attempt + 1}`,
    method: "POST",
  });

  if (digit) {
    console.log(`[Menu] Invalid digit received: "${digit}"`);
    gather.say("Sorry, that's not a valid option.");
  }

  const optionKeys = Object.keys(config.options || {}).sort();
  for (const key of optionKeys) {
    gather.say(`Press ${key} for ${config.options[key].label}.`);
  }

  twiml.redirect(`/voice?attempt=${attempt + 1}`);
  return callback(null, twiml);
};