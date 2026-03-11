# Stripe Quick Setup (FunRobin -> New Project)

This guide extracts the full Stripe subscription flow from FunRobin so you can transplant it into a new Express + Mongo + React project with minimal guesswork. It includes backend Stripe lifecycle handling, frontend subscription UX wiring, premium-gate enforcement, and reconciliation logic.

## 1) Copy Map

Use these source files as the canonical implementation map:

- [subscriptionController.js](/Users/stephenekwedike/Downloads/FunRobin-dev/server/controllers/subscriptionController.js)
- [subscriptionRouter.js](/Users/stephenekwedike/Downloads/FunRobin-dev/server/routes/subscriptionRouter.js)
- [index.js](/Users/stephenekwedike/Downloads/FunRobin-dev/server/index.js)
- [subscriptionSyncJob.js](/Users/stephenekwedike/Downloads/FunRobin-dev/server/jobs/subscriptionSyncJob.js)
- [userModel.js](/Users/stephenekwedike/Downloads/FunRobin-dev/server/models/userModel.js)
- [auth.js](/Users/stephenekwedike/Downloads/FunRobin-dev/server/middleware/auth.js)
- [subscriptionServices.js](/Users/stephenekwedike/Downloads/FunRobin-dev/client/src/services/subscriptionServices.js)
- [Pricing/index.jsx](/Users/stephenekwedike/Downloads/FunRobin-dev/client/src/pages/client/Pricing/index.jsx)
- [SubscriptionSuccess/index.jsx](/Users/stephenekwedike/Downloads/FunRobin-dev/client/src/pages/client/SubscriptionSuccess/index.jsx)
- [SubscriptionManage/index.jsx](/Users/stephenekwedike/Downloads/FunRobin-dev/client/src/pages/client/SubscriptionManage/index.jsx)
- [navigation/index.jsx](/Users/stephenekwedike/Downloads/FunRobin-dev/client/src/navigation/index.jsx)

## 2) Environment + Dependencies

Required server env vars:

```env
STRIPE_SECRET_KEY=your_stripe_secret_key_here
STRIPE_WEBHOOK_SECRET=your_stripe_webhook_secret_here
STRIPE_PREMIUM_PRICE_ID=your_stripe_premium_price_id_here
```

Optional but present in source:

```env
STRIPE_PUBLISHABLE_KEY=your_stripe_publishable_key_here
STRIPE_TEST_AUTO_PAY=true
```

Server dependencies:

```json
{
  "dependencies": {
    "stripe": "^19.3.0",
    "node-cron": "^3.0.3"
  }
}
```

## 3) Server Wiring

Make sure raw webhook body parsing happens before JSON body parsing.

```js
// Stripe webhook route - must be before body parser to get raw body
app.use("/api/subscriptions/webhook", express.raw({ type: "application/json" }));

app.use(bodyParser.json());
app.use(bodyParser.urlencoded({ extended: true }));

app.use("/api/subscriptions", subscriptionRouter);

// Initialize subscription sync job to verify subscription statuses with Stripe daily
initSubscriptionSyncJob();
```

## 4) Route Layer

```js
router.post("/create-checkout-session", isAutheticated, createCheckoutSession);
router.get("/verify-session/:sessionId", isAutheticated, verifyCheckoutSession);
router.get("/status", isAutheticated, getSubscriptionStatus);
router.post("/cancel", isAutheticated, cancelSubscription);
router.post("/resume", isAutheticated, resumeSubscription);
router.post("/webhook", handleStripeWebhook);
```

## 5) Data Model

Subscription fields from `userSchema`, with `subscriptionExpiry` added for portability because it is used across middleware/controllers/jobs.

```js
isPremium: {
  type: Boolean,
  default: false,
},

stripeCustomerId: {
  type: String,
  default: null,
},
stripeSubscriptionId: {
  type: String,
  default: null,
},
subscriptionStatus: {
  type: String,
  enum: ["active", "inactive", "canceled", "past_due", "trialing"],
  default: "inactive",
},
subscriptionPlan: {
  type: String,
  enum: ["premium", "free"],
  default: "free",
},
subscriptionStartDate: {
  type: Date,
  default: null,
},
subscriptionEndDate: {
  type: Date,
  default: null,
},
trialEndDate: {
  type: Date,
  default: null,
},

// Add this explicitly in new project for consistency with runtime checks
subscriptionExpiry: {
  type: Date,
  default: null,
},
```

## 6) Controller Snippets

Use these as your baseline backend flow.

```js
import Stripe from "stripe";
import catchAsyncError from "../middleware/catchAsyncErrors.js";
import ErrorHandler from "../utils/errorHandler.js";
import User from "../models/userModel.js";

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY);
const PREMIUM_PRICE_ID = process.env.STRIPE_PREMIUM_PRICE_ID;

function convertUnixToDate(unixTimestamp) {
  if (!unixTimestamp) return null;
  try {
    const date = new Date(unixTimestamp * 1000);
    if (isNaN(date.getTime())) return null;
    return date;
  } catch {
    return null;
  }
}

export const createCheckoutSession = catchAsyncError(async (req, res, next) => {
  try {
    const userId = req.user?.id;
    if (!userId) return next(new ErrorHandler("User not authenticated", 401));

    const plan = "premium";
    const user = await User.findById(userId);
    if (!user) return next(new ErrorHandler("User not found", 404));

    let customerId = user.stripeCustomerId;
    if (!customerId) {
      const customer = await stripe.customers.create({
        email: user.email,
        name: user.name,
        metadata: { userId: user._id.toString() },
      });
      customerId = customer.id;
      user.stripeCustomerId = customerId;
      user.subscriptionPlan = plan;
      await user.save();
    }

    const priceId = PREMIUM_PRICE_ID;
    if (!priceId) return next(new ErrorHandler("Stripe price ID not configured", 500));

    const session = await stripe.checkout.sessions.create({
      customer: customerId,
      payment_method_types: ["card"],
      line_items: [{ price: priceId, quantity: 1 }],
      mode: "subscription",
      success_url: `${process.env.CLIENT_BASE_URL}/subscription/success?session_id={CHECKOUT_SESSION_ID}`,
      cancel_url: `${process.env.CLIENT_BASE_URL}/subscription/cancel`,
      metadata: {
        userId: user._id.toString(),
        plan,
      },
      subscription_data: {
        trial_period_days: 3,
        metadata: {
          userId: user._id.toString(),
          plan,
        },
      },
    });

    res.status(200).json({
      success: true,
      sessionId: session.id,
      url: session.url,
    });
  } catch (error) {
    return next(new ErrorHandler(error.message || "Failed to create checkout session", 500));
  }
});

export const verifyCheckoutSession = catchAsyncError(async (req, res, next) => {
  try {
    const { sessionId } = req.params;
    const userId = req.user?.id;

    if (!userId) return next(new ErrorHandler("User not authenticated", 401));

    const session = await stripe.checkout.sessions.retrieve(sessionId);
    if (session.metadata.userId !== userId) {
      return next(new ErrorHandler("Session does not belong to this user", 403));
    }

    const subscription = await stripe.subscriptions.retrieve(session.subscription);
    const user = await User.findById(userId);
    if (!user) return next(new ErrorHandler("User not found", 404));

    user.stripeSubscriptionId = subscription.id;
    user.subscriptionStatus = subscription.status;
    user.subscriptionPlan = session.metadata.plan;
    user.isPremium = true;

    const startDate = convertUnixToDate(subscription.current_period_start);
    const endDate = convertUnixToDate(subscription.current_period_end);
    const trialEnd = convertUnixToDate(subscription.trial_end);

    if (startDate) user.subscriptionStartDate = startDate;
    if (endDate) {
      user.subscriptionEndDate = endDate;
      user.subscriptionExpiry = endDate;
    }
    if (trialEnd) user.trialEndDate = trialEnd;

    await user.save();

    res.status(200).json({
      success: true,
      message: "Subscription activated successfully",
      subscription: {
        id: subscription.id,
        status: subscription.status,
        plan: session.metadata.plan,
        currentPeriodEnd: endDate,
        trialEnd,
      },
    });
  } catch (error) {
    return next(new ErrorHandler(error.message || "Failed to verify session", 500));
  }
});

export const getSubscriptionStatus = catchAsyncError(async (req, res, next) => {
  try {
    const userId = req.user?.id;
    if (!userId) return next(new ErrorHandler("User not authenticated", 401));

    const user = await User.findById(userId);
    if (!user) return next(new ErrorHandler("User not found", 404));

    let subscriptionDetails = null;

    if (user.stripeSubscriptionId) {
      try {
        const subscription = await stripe.subscriptions.retrieve(user.stripeSubscriptionId);

        subscriptionDetails = {
          id: subscription.id,
          status: subscription.status,
          plan: user.subscriptionPlan,
          currentPeriodStart: convertUnixToDate(subscription.current_period_start),
          currentPeriodEnd: convertUnixToDate(subscription.current_period_end),
          trialEnd: convertUnixToDate(subscription.trial_end),
          cancelAtPeriodEnd: subscription.cancel_at_period_end,
        };

        if (user.subscriptionStatus !== subscription.status) {
          user.subscriptionStatus = subscription.status;
          await user.save();
        }
      } catch (error) {
        console.error("Error fetching subscription from Stripe:", error);
      }
    }

    res.status(200).json({
      success: true,
      subscription: subscriptionDetails,
      user: {
        subscriptionPlan: user.subscriptionPlan,
        subscriptionStatus: user.subscriptionStatus,
        subscriptionEndDate: user.subscriptionEndDate,
        trialEndDate: user.trialEndDate,
        isPremium: user.isPremium,
      },
    });
  } catch (error) {
    return next(new ErrorHandler(error.message || "Failed to get subscription status", 500));
  }
});

export const cancelSubscription = catchAsyncError(async (req, res, next) => {
  try {
    const userId = req.user?.id;
    if (!userId) return next(new ErrorHandler("User not authenticated", 401));

    const user = await User.findById(userId);
    if (!user) return next(new ErrorHandler("User not found", 404));
    if (!user.stripeSubscriptionId) return next(new ErrorHandler("No active subscription found", 404));

    const subscription = await stripe.subscriptions.update(user.stripeSubscriptionId, {
      cancel_at_period_end: true,
    });

    user.subscriptionStatus = "canceled";
    user.isPremium = false;
    await user.save();

    res.status(200).json({
      success: true,
      message: "Subscription will be canceled at the end of the billing period",
      subscription: {
        id: subscription.id,
        status: subscription.status,
        cancelAtPeriodEnd: subscription.cancel_at_period_end,
        currentPeriodEnd: convertUnixToDate(subscription.current_period_end),
      },
    });
  } catch (error) {
    return next(new ErrorHandler(error.message || "Failed to cancel subscription", 500));
  }
});

export const resumeSubscription = catchAsyncError(async (req, res, next) => {
  try {
    const userId = req.user?.id;
    if (!userId) return next(new ErrorHandler("User not authenticated", 401));

    const user = await User.findById(userId);
    if (!user) return next(new ErrorHandler("User not found", 404));
    if (!user.stripeSubscriptionId) return next(new ErrorHandler("No subscription found", 404));

    const subscription = await stripe.subscriptions.update(user.stripeSubscriptionId, {
      cancel_at_period_end: false,
    });

    user.subscriptionStatus = subscription.status;
    user.isPremium = true;
    await user.save();

    res.status(200).json({
      success: true,
      message: "Subscription resumed successfully",
      subscription: {
        id: subscription.id,
        status: subscription.status,
        cancelAtPeriodEnd: subscription.cancel_at_period_end,
        currentPeriodEnd: convertUnixToDate(subscription.current_period_end),
      },
    });
  } catch (error) {
    return next(new ErrorHandler(error.message || "Failed to resume subscription", 500));
  }
});
```

## 7) Webhook Snippets

Signature verification + event switch + handlers.

```js
export const handleStripeWebhook = catchAsyncError(async (req, res) => {
  const sig = req.headers["stripe-signature"];
  const webhookSecret = process.env.STRIPE_WEBHOOK_SECRET;

  let event;

  try {
    event = stripe.webhooks.constructEvent(req.body, sig, webhookSecret);
  } catch (err) {
    return res.status(400).send(`Webhook Error: ${err.message}`);
  }

  try {
    switch (event.type) {
      case "checkout.session.completed":
        await handleCheckoutSessionCompleted(event.data.object);
        break;
      case "customer.subscription.created":
        await handleSubscriptionCreated(event.data.object);
        break;
      case "customer.subscription.updated":
        await handleSubscriptionUpdated(event.data.object);
        break;
      case "customer.subscription.deleted":
        await handleSubscriptionDeleted(event.data.object);
        break;
      case "invoice.payment_succeeded":
        await handleInvoicePaymentSucceeded(event.data.object);
        break;
      case "invoice.payment_failed":
        await handleInvoicePaymentFailed(event.data.object);
        break;
      default:
        console.log(`Unhandled event type ${event.type}`);
    }

    res.json({ received: true });
  } catch {
    return res.status(500).json({ error: "Webhook handler failed" });
  }
});

async function handleCheckoutSessionCompleted(session) {
  const userId = session.metadata.userId;
  const user = await User.findById(userId);

  if (user && session.subscription) {
    const subscription = await stripe.subscriptions.retrieve(session.subscription);

    user.stripeSubscriptionId = subscription.id;
    user.subscriptionStatus = subscription.status;
    user.subscriptionPlan = session.metadata.plan;
    user.isPremium = true;

    const startDate = convertUnixToDate(subscription.current_period_start);
    const endDate = convertUnixToDate(subscription.current_period_end);
    const trialEnd = convertUnixToDate(subscription.trial_end);

    if (startDate) user.subscriptionStartDate = startDate;
    if (endDate) {
      user.subscriptionEndDate = endDate;
      user.subscriptionExpiry = endDate;
    }
    if (trialEnd) user.trialEndDate = trialEnd;

    await user.save();
  }
}

async function handleSubscriptionCreated(subscription) {
  const userId = subscription.metadata.userId;
  const user = await User.findById(userId);

  if (user) {
    user.stripeSubscriptionId = subscription.id;
    user.subscriptionStatus = subscription.status;
    user.isPremium = true;

    const startDate = convertUnixToDate(subscription.current_period_start);
    const endDate = convertUnixToDate(subscription.current_period_end);
    const trialEnd = convertUnixToDate(subscription.trial_end);

    if (startDate) user.subscriptionStartDate = startDate;
    if (endDate) {
      user.subscriptionEndDate = endDate;
      user.subscriptionExpiry = endDate;
    }
    if (trialEnd) user.trialEndDate = trialEnd;

    await user.save();
  }
}

async function handleSubscriptionUpdated(subscription) {
  const user = await User.findOne({ stripeSubscriptionId: subscription.id });

  if (user) {
    user.subscriptionStatus = subscription.status;
    user.isPremium = subscription.status === "active" || subscription.status === "trialing";

    const endDate = convertUnixToDate(subscription.current_period_end);
    const trialEnd = convertUnixToDate(subscription.trial_end);

    if (endDate) {
      user.subscriptionEndDate = endDate;
      user.subscriptionExpiry = endDate;
    }
    if (trialEnd) user.trialEndDate = trialEnd;

    await user.save();
  }
}

async function handleSubscriptionDeleted(subscription) {
  const user = await User.findOne({ stripeSubscriptionId: subscription.id });

  if (user) {
    user.subscriptionStatus = "canceled";
    user.subscriptionPlan = "free";
    user.isPremium = false;
    await user.save();
  }
}

async function handleInvoicePaymentSucceeded(invoice) {
  if (invoice.subscription) {
    const subscription = await stripe.subscriptions.retrieve(invoice.subscription);
    const user = await User.findOne({ stripeSubscriptionId: subscription.id });

    if (user) {
      user.subscriptionStatus = "active";
      user.isPremium = true;

      const endDate = convertUnixToDate(subscription.current_period_end);
      if (endDate) {
        user.subscriptionEndDate = endDate;
        user.subscriptionExpiry = endDate;
      }

      await user.save();
    }
  }
}

async function handleInvoicePaymentFailed(invoice) {
  if (invoice.subscription) {
    const user = await User.findOne({ stripeSubscriptionId: invoice.subscription });

    if (user) {
      user.subscriptionStatus = "past_due";
      user.isPremium = false;
      await user.save();
    }
  }
}
```

## 8) Reconciliation Job Snippets

Daily Stripe reconciliation (important for drift correction).

```js
import cron from "node-cron";
import Stripe from "stripe";
import User from "../models/userModel.js";

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY);

const syncSubscriptionStatus = async () => {
  const usersWithSubscriptions = await User.find({
    stripeSubscriptionId: { $exists: true, $ne: null },
  }).select("_id email stripeSubscriptionId subscriptionStatus isPremium subscriptionEndDate subscriptionStartDate trialEndDate");

  for (const user of usersWithSubscriptions) {
    try {
      const subscription = await stripe.subscriptions.retrieve(user.stripeSubscriptionId);
      const shouldBePremium = subscription.status === "active" || subscription.status === "trialing";

      const needsUpdate =
        user.subscriptionStatus !== subscription.status ||
        user.isPremium !== shouldBePremium;

      if (needsUpdate) {
        user.subscriptionStatus = subscription.status;
        user.isPremium = shouldBePremium;

        const startDate = convertUnixToDate(subscription.current_period_start);
        const endDate = convertUnixToDate(subscription.current_period_end);
        const trialEnd = convertUnixToDate(subscription.trial_end);

        if (startDate) user.subscriptionStartDate = startDate;
        if (endDate) {
          user.subscriptionEndDate = endDate;
          user.subscriptionExpiry = endDate;
        }
        if (trialEnd) user.trialEndDate = trialEnd;

        if (
          subscription.status === "canceled" ||
          subscription.status === "unpaid" ||
          subscription.status === "incomplete_expired"
        ) {
          user.isPremium = false;
          user.subscriptionPlan = "free";
        }

        if (subscription.status === "past_due") {
          user.isPremium = false;
        }

        await user.save();
      }
    } catch (error) {
      if (error.type === "StripeInvalidRequestError" && error.code === "resource_missing") {
        user.subscriptionStatus = "canceled";
        user.isPremium = false;
        user.subscriptionPlan = "free";
        await user.save();
      }
    }
  }
};

export const initSubscriptionSyncJob = () => {
  const cronExpression = "0 3 * * *";
  return cron.schedule(cronExpression, syncSubscriptionStatus, {
    scheduled: true,
    timezone: "UTC",
  });
};
```

## 9) Frontend Snippets

### API service helpers (`access-token` + `VITE_REACT_APP_API_BASE_URL`)

```js
import axios from "axios";

const API_BASE_URL = import.meta.env.VITE_REACT_APP_API_BASE_URL;

export const createCheckoutSessionRequest = (accessToken) => {
  return axios.post(
    `${API_BASE_URL}/api/subscriptions/create-checkout-session`,
    {},
    {
      headers: {
        "Content-Type": "application/json",
        "access-token": accessToken,
      },
      withCredentials: true,
    }
  );
};

export const verifyCheckoutSessionRequest = (sessionId, accessToken) => {
  return axios.get(`${API_BASE_URL}/api/subscriptions/verify-session/${sessionId}`, {
    headers: {
      "Content-Type": "application/json",
      "access-token": accessToken,
    },
    withCredentials: true,
  });
};

export const getSubscriptionStatusRequest = (accessToken) => {
  return axios.get(`${API_BASE_URL}/api/subscriptions/status`, {
    headers: {
      "Content-Type": "application/json",
      "access-token": accessToken,
    },
    withCredentials: true,
  });
};

export const cancelSubscriptionRequest = (accessToken) => {
  return axios.post(`${API_BASE_URL}/api/subscriptions/cancel`, {}, {
    headers: {
      "Content-Type": "application/json",
      "access-token": accessToken,
    },
    withCredentials: true,
  });
};

export const resumeSubscriptionRequest = (accessToken) => {
  return axios.post(`${API_BASE_URL}/api/subscriptions/resume`, {}, {
    headers: {
      "Content-Type": "application/json",
      "access-token": accessToken,
    },
    withCredentials: true,
  });
};
```

### Pricing checkout redirect

```js
const handleSubscribe = async () => {
  try {
    setLoading(true);

    if (!isAuthenticated || !token) {
      toast.error("Please login to subscribe");
      navigate("/login");
      return;
    }

    const response = await createCheckoutSessionRequest(token);

    if (response.data.success && response.data.url) {
      window.location.href = response.data.url;
    } else {
      toast.error("Failed to create checkout session");
    }
  } catch (error) {
    toast.error(error.response?.data?.message || "Failed to start subscription process");
  } finally {
    setLoading(false);
  }
};
```

### Success page verification

```js
const sessionId = searchParams.get("session_id");

if (!sessionId) {
  toast.error("Invalid session");
  navigate("/pricing");
  return;
}

if (!token) {
  toast.error("Please login to verify subscription");
  navigate("/login");
  return;
}

const response = await verifyCheckoutSessionRequest(sessionId, token);
if (response.data.success) {
  setVerified(true);
  toast.success("Subscription activated successfully!");
}
```

### Manage subscription actions

```js
const fetchSubscriptionStatus = async () => {
  const response = await getSubscriptionStatusRequest(token);
  if (response.data.success) {
    setSubscription(response.data);
  }
};

const handleCancelSubscription = async () => {
  const response = await cancelSubscriptionRequest(token);
  if (response.data.success) {
    toast.success("Subscription will be cancelled at the end of billing period");
    fetchSubscriptionStatus();
  }
};

const handleResumeSubscription = async () => {
  const response = await resumeSubscriptionRequest(token);
  if (response.data.success) {
    toast.success("Subscription resumed successfully");
    fetchSubscriptionStatus();
  }
};
```

### Route registration

```jsx
<Route path="/pricing" element={<Pricing />} />
<Route path="/subscription/success" element={<SubscriptionSuccess />} />
<Route path="/subscription/cancel" element={<SubscriptionCancel />} />
<Route path="/subscription/manage" element={<SubscriptionManage />} />
```

## 10) Premium Gate Snippets

Middleware and guarded routes/controllers relying on subscription state.

```js
export const isPremium = catchAsyncError(async (req, res, next) => {
  const userId = req.user?.id;

  if (!userId) {
    return next(new ErrorHandler("User not authenticated", 401));
  }

  const user = await User.findById(userId);

  if (!user) {
    return next(new ErrorHandler("User not found", 404));
  }

  if (!user.isPremium) {
    return next(new ErrorHandler("Premium subscription required to access this feature", 403));
  }

  if (user.subscriptionExpiry && new Date(user.subscriptionExpiry) < new Date()) {
    return next(new ErrorHandler("Your premium subscription has expired. Please renew to access this feature", 403));
  }

  next();
});
```

```js
// Leaderboard routes
router.get("/", isAutheticated, isPremium, getLeaderboard);
router.get("/me", isAutheticated, isPremium, getUserLeaderboardPosition);
```

```js
// Contract premium checks
if (contract.isHot && !user.isPremium) {
  return next(new ErrorHandler("Premium subscription required for hot contracts", 403));
}

if (contract.multiplier > 10 && !user.isPremium) {
  return next(new ErrorHandler("Premium subscription required for high multiplier contracts", 403));
}

if (user.isPremium && user.subscriptionExpiry && new Date(user.subscriptionExpiry) < new Date()) {
  return next(new ErrorHandler("Your premium subscription has expired", 403));
}
```

## 11) Test Checklist

Run through these scenarios after transplant:

1. Authenticated user starts checkout and receives a Stripe Checkout URL.
2. Stripe success redirect to `/subscription/success` verifies session and persists `stripeSubscriptionId`, status, plan, and dates.
3. Stripe cancel redirect to `/subscription/cancel` leaves user without premium activation.
4. Webhook `checkout.session.completed` updates user subscription state.
5. Webhook `invoice.payment_failed` sets status to `past_due` and disables premium access.
6. `POST /api/subscriptions/cancel` sets `cancel_at_period_end=true`; `POST /api/subscriptions/resume` reverses it.
7. Premium-protected endpoints return `403` for non-premium users.
8. Daily reconciliation job fixes stale DB status when Stripe state changes outside your app.

## 12) Known Issues + Fixes

1. Immediate premium removal on cancel-at-period-end:
Current code sets `isPremium = false` immediately in cancel flow, even though access may continue until period end. In new project, set premium based on Stripe status + period end.

2. Unconditional `isPremium=true` in verify flow:
`verifyCheckoutSession` currently marks premium true regardless of returned Stripe status. Safer implementation is `isPremium = status === "active" || status === "trialing"`.

3. Trial copy mismatch:
Checkout creates a 3-day trial (`trial_period_days: 3`) while some UI copy mentions 14 days. Centralize trial days in config and reuse that value across UI and backend.

4. Duplicate status update paths:
Status mutates in verify endpoint, webhook handlers, and daily sync job. Keep this if you want defense-in-depth, but make handlers idempotent and document source-of-truth precedence.

## Assumptions / Defaults

1. Target system is Express + Mongo + React.
2. Reuse keeps current endpoint names and `access-token` contract.
3. Include both webhook and cron reconciliation in the new project.
4. This is documentation-only and does not modify FunRobin source code.
