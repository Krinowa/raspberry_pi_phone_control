"""Wake the manually unlocked phone and open Settings. No coordinates or input."""


def run(context):
    context.send("screen_on")
    context.send("home")
    context.launch_app("com.android.settings")
    context.wait(1.0)
