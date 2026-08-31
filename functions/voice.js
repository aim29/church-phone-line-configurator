// voice.js
//
// Deployed as a Twilio Function alongside the church's audio Assets.
// Reads its call-flow config from the CONFIG_JSON environment variable
// (set by the provisioning app) and returns TwiML.
//
// CONFIG_JSON shape, single-recording mode:
//   {
//     "mode": "single",
//     "welcome_path": "/welcome.mp3",        // one of welcome_path / welcome_tts
//     "welcome_tts": "Thank you for calling...",
//     "recording_path": "/recording.mp3"
//   }
//
// CONFIG_JSON shape, menu mode:
//   {
//     "mode": "menu",
//     "welcome_path": "/welcome.mp3",        // one of welcome_path / welcome_tts
//     "welcome_tts": "Thank you for calling...",
//     "announce_options": true,              // false if the welcome message
//                                             // already lists the options
//     "options": {
//       "1": { "label": "the Sunday sermon", "path": "/option-1.mp3" },
//       "2": { "label": "this week's notices", "path": "/option-2.mp3" }
//     }
//   }

const MAX_ATTEMPTS = 3;

// All synthesized speech uses a British English voice. "woman" is one of
// Twilio's Basic-tier voices (free, no Polly/Google usage charge) and,
// paired with language "en-GB", speaks with a British accent. There is
// no Scottish-accented voice in Twilio's TTS catalog at any tier — Basic
// only offers generic "man"/"woman", and Polly/Google's "English (UK)"
// voices (Amy, Emma, Brian, Arthur, etc.) are likewise a generic
// Southern-England accent, not Scottish.
const VOICE = { voice: "woman", language: "en-GB" };

// Helper to build fully qualified public asset URLs
function getPublicAssetUrl(context, path) {
  if (!path) return null;
  const cleanPath = path.startsWith('/') ? path : `/${path}`;
  const url = `https://${context.DOMAIN_NAME}${cleanPath}`;
  console.log(`[Asset URL] Formatted: ${url}`);
  return url;
}

// Speaks the welcome message onto `target` (either the top-level twiml
// response, or a <Gather> — both expose .say()/.play()), using whichever
// of welcome_tts / welcome_path is set.
function sayWelcome(target, context, config) {
  if (config.welcome_tts) {
    console.log('[Welcome] Using TTS.');
    target.say(VOICE, config.welcome_tts);
  } else if (config.welcome_path) {
    const url = getPublicAssetUrl(context, config.welcome_path);
    if (url) target.play(url);
  }
}

// Reads out each menu option ("Press 1 for the Sunday sermon...") onto
// `target` (typically a <Gather>).
function announceOptions(target, config) {
  const optionKeys = Object.keys(config.options || {}).sort();
  for (const key of optionKeys) {
    target.say(VOICE, `Press ${key} for ${config.options[key].label}.`);
  }
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
    twiml.say(VOICE, "Sorry, this phone line has not been configured yet.");
    twiml.hangup();
    return callback(null, twiml);
  }

  const digit = event.Digits;
  const attempt = parseInt(event.attempt || "0", 10);
  const isFirstRequest = !digit && attempt === 0;
  const announceOptionsEnabled = config.announce_options !== false;

  console.log(`[Call State] Digit: ${digit || 'None'}, Attempt: ${attempt}, First Request: ${isFirstRequest}`);

  if (config.mode === "single") {
    console.log('[Flow] Single-recording mode.');
    if (isFirstRequest) {
      sayWelcome(twiml, context, config);
    }
    const recordingUrl = getPublicAssetUrl(context, config.recording_path);
    if (recordingUrl) {
      twiml.play(recordingUrl);
    } else {
      twiml.say(VOICE, "Sorry, no recording is available at the moment.");
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
      twiml.say(VOICE, "Sorry, that recording is not available at the moment.");
    }
    twiml.hangup();
    return callback(null, twiml);
  }

  if (attempt >= MAX_ATTEMPTS) {
    console.warn(`[Menu] Max retry attempts (${MAX_ATTEMPTS}) reached.`);
    twiml.say(VOICE, "Sorry, no valid option was selected. Goodbye.");
    twiml.hangup();
    return callback(null, twiml);
  }

  // The welcome message (and, on first request, the menu announcement)
  // is nested INSIDE the <Gather> rather than played beforehand. This
  // means Twilio starts listening for a key press from the very start
  // of the welcome message, not only once it finishes — a caller who
  // already knows the menu can press a digit straight away.
  const gather = twiml.gather({
    numDigits: 1,
    timeout: 6,
    action: `/voice?attempt=${attempt + 1}`,
    method: "POST",
  });

  if (isFirstRequest) {
    sayWelcome(gather, context, config);
    if (announceOptionsEnabled) {
      announceOptions(gather, config);
    }
  } else {
    if (digit) {
      console.log(`[Menu] Invalid digit received: "${digit}"`);
      gather.say(VOICE, "Sorry, that's not a valid option.");
    }
    if (announceOptionsEnabled) {
      announceOptions(gather, config);
    }
  }

  // If Gather times out with no input, Twilio falls through to here.
  twiml.redirect(`/voice?attempt=${attempt + 1}`);
  return callback(null, twiml);
};
