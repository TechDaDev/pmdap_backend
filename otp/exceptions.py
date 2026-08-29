class OtpError(Exception):
    pass


class InvalidOtp(OtpError):
    pass


class OtpCooldown(OtpError):
    def __init__(self, retry_after_seconds):
        self.retry_after_seconds = retry_after_seconds
        super().__init__("OTP resend cooldown is active.")


class OtpRateLimited(OtpError):
    pass


class OtpDeliveryFailed(OtpError):
    pass


class UnsupportedOtpChannel(OtpError):
    pass
