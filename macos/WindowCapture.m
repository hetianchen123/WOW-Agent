#import <CoreGraphics/CoreGraphics.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#import <Foundation/Foundation.h>
#import <ScreenCaptureKit/ScreenCaptureKit.h>
#import <mach/mach_time.h>

@interface WindowCapture : NSObject <SCStreamOutput, SCStreamDelegate> {
    SCStream *_stream;
    CVPixelBufferRef _latest;
    uint64_t _displayTime;
    uint64_t _sequence;
    size_t _width, _height;
    BOOL _closed, _failed;
}
- (instancetype)initWithWindow:(CGWindowID)windowID width:(size_t)width height:(size_t)height;
- (int)copyX:(size_t)x y:(size_t)y width:(size_t)width height:(size_t)height
      output:(uint8_t *)output age:(double *)age sequence:(uint64_t *)sequence;
- (void)close;
@end

@implementation WindowCapture
- (instancetype)initWithWindow:(CGWindowID)windowID width:(size_t)width height:(size_t)height {
    self = [super init];
    if (!self) return nil;
    _width = width;
    _height = height;
    [SCShareableContent getShareableContentExcludingDesktopWindows:YES onScreenWindowsOnly:YES
          completionHandler:^(SCShareableContent *content, NSError *error) {
        @synchronized (self) {
            if (self->_closed) return;
            SCWindow *selected = nil;
            for (SCWindow *window in content.windows) {
                if (window.windowID == windowID) {
                    selected = window;
                    break;
                }
            }
            if (error || !selected) {
                self->_failed = YES;
                return;
            }
            SCContentFilter *filter = [[SCContentFilter alloc] initWithDesktopIndependentWindow:selected];
            SCStreamConfiguration *config = [[SCStreamConfiguration alloc] init];
            config.width = width;
            config.height = height;
            config.captureResolution = SCCaptureResolutionNominal;
            config.ignoreShadowsSingleWindow = YES;
            config.showsCursor = NO;
            config.minimumFrameInterval = CMTimeMake(1, 60);
            config.queueDepth = 3;
            config.pixelFormat = kCVPixelFormatType_32BGRA;
            config.colorSpaceName = kCGColorSpaceSRGB;
            self->_stream = [[SCStream alloc] initWithFilter:filter configuration:config delegate:self];
            dispatch_queue_t queue = dispatch_queue_create("io.wowagent.capture.latest", DISPATCH_QUEUE_SERIAL);
            if (![self->_stream addStreamOutput:self type:SCStreamOutputTypeScreen sampleHandlerQueue:queue error:&error]) {
                self->_failed = YES;
                self->_stream = nil;
                return;
            }
            [self->_stream startCaptureWithCompletionHandler:^(NSError *startError) {
                if (startError) {
                    @synchronized (self) { self->_failed = YES; }
                }
            }];
        }
    }];
    return self;
}

- (void)stream:(SCStream *)stream didOutputSampleBuffer:(CMSampleBufferRef)sample ofType:(SCStreamOutputType)type {
    if (type != SCStreamOutputTypeScreen || !CMSampleBufferDataIsReady(sample)) return;
    NSArray *attachments = (__bridge NSArray *)CMSampleBufferGetSampleAttachmentsArray(sample, false);
    NSDictionary *info = attachments.firstObject;
    NSNumber *status = info[SCStreamFrameInfoStatus];
    NSNumber *timestamp = info[SCStreamFrameInfoDisplayTime];
    if (!status || status.integerValue != SCFrameStatusComplete) {
        if (status && status.integerValue != SCFrameStatusIdle) {
            @synchronized (self) {
                if (_latest) CVPixelBufferRelease(_latest);
                _latest = NULL;
            }
        }
        return;
    }
    if (!timestamp.unsignedLongLongValue) return;

    CVPixelBufferRef pixels = CMSampleBufferGetImageBuffer(sample);
    CGRect contentRect;
    NSDictionary *content = info[SCStreamFrameInfoContentRect];
    CGFloat scale = [info[SCStreamFrameInfoScaleFactor] doubleValue];
    if (!pixels || CVPixelBufferGetWidth(pixels) != _width || CVPixelBufferGetHeight(pixels) != _height ||
        CVPixelBufferGetPixelFormatType(pixels) != kCVPixelFormatType_32BGRA ||
        !content || !CGRectMakeWithDictionaryRepresentation((__bridge CFDictionaryRef)content, &contentRect) ||
        scale <= 0 || fabs(contentRect.origin.x * scale) > 1 || fabs(contentRect.origin.y * scale) > 1 ||
        fabs(contentRect.size.width * scale - _width) > 1 || fabs(contentRect.size.height * scale - _height) > 1) return;

    @synchronized (self) {
        if (_closed || _failed || timestamp.unsignedLongLongValue <= _displayTime) return;
        if (_latest) CVPixelBufferRelease(_latest);
        _latest = CVPixelBufferRetain(pixels);
        _displayTime = timestamp.unsignedLongLongValue;
        _sequence++;
    }
}

- (void)stream:(SCStream *)stream didStopWithError:(NSError *)error {
    @synchronized (self) { _failed = YES; }
}

- (int)copyX:(size_t)x y:(size_t)y width:(size_t)width height:(size_t)height
      output:(uint8_t *)output age:(double *)age sequence:(uint64_t *)sequence {
    if (!output || !age || !sequence || !width || !height || x > _width || y > _height ||
        width > _width - x || height > _height - y) return -1;
    CVPixelBufferRef pixels;
    @synchronized (self) {
        if (_closed || _failed) return -1;
        if (!_latest) return 0;
        uint64_t now = mach_absolute_time();
        if (now < _displayTime) return 0;
        *age = CMTimeGetSeconds(CMClockMakeHostTimeFromSystemUnits(now - _displayTime));
        if (*age > .12) return 0;
        *sequence = _sequence;
        pixels = CVPixelBufferRetain(_latest);
    }
    if (CVPixelBufferLockBaseAddress(pixels, kCVPixelBufferLock_ReadOnly) != kCVReturnSuccess) {
        CVPixelBufferRelease(pixels);
        return 0;
    }
    size_t stride = CVPixelBufferGetBytesPerRow(pixels);
    const uint8_t *source = CVPixelBufferGetBaseAddress(pixels);
    if (source) {
        for (size_t row = 0; row < height; row++)
            memcpy(output + row * width * 4, source + (y + row) * stride + x * 4, width * 4);
    }
    CVPixelBufferUnlockBaseAddress(pixels, kCVPixelBufferLock_ReadOnly);
    CVPixelBufferRelease(pixels);
    return source ? 1 : 0;
}

- (void)close {
    SCStream *stream;
    @synchronized (self) {
        if (_closed) return;
        _closed = YES;
        stream = _stream;
        _stream = nil;
        if (_latest) CVPixelBufferRelease(_latest);
        _latest = NULL;
    }
    [stream stopCaptureWithCompletionHandler:nil];
}
@end

void *wa_capture_start(uint32_t window, size_t width, size_t height) {
    @autoreleasepool {
        if (!window || !width || !height || !CGPreflightScreenCaptureAccess()) return NULL;
        return (__bridge_retained void *)[[WindowCapture alloc] initWithWindow:window width:width height:height];
    }
}

int wa_capture_copy(void *handle, size_t x, size_t y, size_t width, size_t height,
                    uint8_t *output, double *age, uint64_t *sequence) {
    @autoreleasepool {
        WindowCapture *capture = (__bridge WindowCapture *)handle;
        return [capture copyX:x y:y width:width height:height output:output age:age sequence:sequence];
    }
}

void wa_capture_stop(void *handle) {
    @autoreleasepool {
        WindowCapture *capture = (__bridge_transfer WindowCapture *)handle;
        [capture close];
    }
}
