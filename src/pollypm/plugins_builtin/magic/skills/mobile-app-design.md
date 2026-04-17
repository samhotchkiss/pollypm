---
name: mobile-app-design
description: Modern mobile UX — gestures, transitions, native feel, one-hand reach, safe areas.
when_to_trigger:
  - mobile design
  - ios design
  - android design
  - app ui
  - native feel
kind: magic_skill
attribution: https://github.com/madewithclaude/awesome-claude-artifacts
---

# Mobile App Design

## When to use

Use when designing for phone screens — native iOS, native Android, React Native, or mobile web that should feel app-like. Mobile has constraints desktop design ignores: thumb reach, safe-area insets, gesture conflicts, network variance. This skill bakes those in from the start.

## Process

1. **Thumb reach first.** Primary actions live in the bottom third of the screen. The top 40% is for viewing, the bottom 40% for acting. Navigation bars at the bottom; hamburger menus lose to bottom tabs every time.
2. **Safe areas, not fixed insets.** Use `env(safe-area-inset-*)` on web, `UIEdgeInsets` on iOS, `WindowInsets` on Android. Never hardcode `padding-top: 44px`. Notches and rounded corners are a moving target.
3. **Gestures first, buttons second.** Swipe-to-dismiss, pull-to-refresh, swipe-for-actions. Offer the button equivalent too — accessibility — but optimize for gesture speed.
4. **Transitions tell a story.** Push animations for forward navigation, modal for step-in, fade for tab change. Never a default wipe in all three cases. iOS uses spring physics; match the platform.
5. **Tap targets 44pt minimum.** iOS HIG, Material guideline. Smaller targets cause misses and misses cause rage. When in doubt, more padding.
6. **Loading states that feel local.** Skeleton screens beat spinners. Skeleton that matches the final layout beats a generic gray bar. Users perceive skeleton-to-content as half the latency of spinner-to-content.
7. **Offline first.** Network variance is the default. Every action that hits the network should queue locally and sync when available. Show the pending state explicitly; do not lie about success.
8. **Type scale for reading at arm's length.** Body 16pt minimum (14pt is desktop-thinking). Headlines 24pt+. Line-height 1.4 for readability on smaller screens.

## Example invocation

```tsx
// React Native — bottom tab nav, safe-area respecting, gesture-first card
import { SafeAreaView } from 'react-native-safe-area-context';
import { GestureDetector, Gesture } from 'react-native-gesture-handler';
import Animated, { useSharedValue, useAnimatedStyle, withSpring } from 'react-native-reanimated';

function TaskCard({ task, onDismiss }) {
  const x = useSharedValue(0);
  const pan = Gesture.Pan()
    .onUpdate(e => { x.value = e.translationX; })
    .onEnd(e => {
      if (Math.abs(e.translationX) > 120) {
        x.value = withSpring(e.translationX > 0 ? 500 : -500);
        onDismiss();
      } else {
        x.value = withSpring(0);
      }
    });

  const style = useAnimatedStyle(() => ({ transform: [{ translateX: x.value }] }));

  return (
    <GestureDetector gesture={pan}>
      <Animated.View style={[{ padding: 20, minHeight: 80 }, style]}>
        <Text style={{ fontSize: 16, lineHeight: 22 }}>{task.title}</Text>
        <Pressable
          onPress={onDismiss}
          style={{ minWidth: 44, minHeight: 44, marginTop: 12 }}
        >
          <Text>Dismiss</Text>
        </Pressable>
      </Animated.View>
    </GestureDetector>
  );
}

function Screen() {
  return (
    <SafeAreaView style={{ flex: 1 }} edges={['top']}>
      <ScrollView>...</ScrollView>
      <BottomTabBar />
    </SafeAreaView>
  );
}
```

## Outputs

- Primary actions in the bottom third.
- Safe-area-aware layout with no hardcoded insets.
- Gestures for frequent actions, buttons as fallback.
- Skeleton loading states that match the final layout.
- 44pt tap targets, 16pt+ body type.

## Common failure modes

- Top-of-screen primary actions; thumb cannot reach one-handed.
- Hardcoded status bar padding; breaks on new devices.
- Spinner-only loading; app feels slower than it is.
- No offline state; users lose work every time the subway tunnel eats their signal.
