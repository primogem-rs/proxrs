// Modified for proxrs: bounded consumption-driven direct-tcpip receive windows.
// See PROXRS_PATCH.md for provenance, behavior, and verification.

use std::borrow::BorrowMut;
use std::io;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{ready, Context, Poll};

use tokio::io::AsyncRead;

use super::{ChannelMsg, ChannelReadHalf};
use crate::channels::ReceiveCredit;

#[derive(Debug)]
pub struct ChannelRx<R> {
    channel: R,
    buffer: Option<(ChannelMsg, usize)>,

    ext: Option<u32>,
    credit: Option<Arc<ReceiveCredit>>,
}

impl<R: BorrowMut<ChannelReadHalf>> ChannelRx<R> {
    pub fn new(mut channel: R, ext: Option<u32>) -> Self {
        let credit = channel.borrow_mut().receive_credit.clone();
        Self {
            channel,
            buffer: None,
            ext,
            credit,
        }
    }
}

impl<R> AsyncRead for ChannelRx<R>
where
    R: BorrowMut<ChannelReadHalf> + Unpin,
{
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut tokio::io::ReadBuf<'_>,
    ) -> Poll<io::Result<()>> {
        if buf.remaining() == 0 { return Poll::Ready(Ok(())); }
        let (msg, mut idx) = match self.buffer.take() {
            Some(msg) => msg,
            None => match ready!(self.channel.borrow_mut().receiver.poll_recv(cx)) {
                Some(msg) => (msg, 0),
                None => return Poll::Ready(Ok(())),
            },
        };

        match (&msg, self.ext) {
            (ChannelMsg::Data { data }, None) => {
                let readable = buf.remaining().min(data.len() - idx);

                // Clamped to maximum `buf.remaining()` and `data.len() - idx` with `.min`
                #[allow(clippy::indexing_slicing)]
                buf.put_slice(&data[idx..idx + readable]);
                idx += readable;
                if let Some(credit) = &self.credit { credit.release(readable); }

                if idx != data.len() {
                    self.buffer = Some((msg, idx));
                }

                Poll::Ready(Ok(()))
            }
            (ChannelMsg::ExtendedData { data, ext }, Some(target)) if *ext == target => {
                let readable = buf.remaining().min(data.len() - idx);

                // Clamped to maximum `buf.remaining()` and `data.len() - idx` with `.min`
                #[allow(clippy::indexing_slicing)]
                buf.put_slice(&data[idx..idx + readable]);
                idx += readable;
                if let Some(credit) = &self.credit { credit.release(readable); }

                if idx != data.len() {
                    self.buffer = Some((msg, idx));
                }

                Poll::Ready(Ok(()))
            }
            (ChannelMsg::Eof, _) => {
                self.channel.borrow_mut().receiver.close();

                Poll::Ready(Ok(()))
            }
            _ => {
                if let Some(credit) = &self.credit {
                    match &msg {
                        ChannelMsg::Data { data } | ChannelMsg::ExtendedData { data, .. } =>
                            credit.release(data.len() - idx),
                        _ => (),
                    }
                }
                cx.waker().wake_by_ref();
                Poll::Pending
            }
        }
    }
}

impl<R> Drop for ChannelRx<R> {
    fn drop(&mut self) {
        if let (
            Some(credit),
            Some((ChannelMsg::Data { data } | ChannelMsg::ExtendedData { data, .. }, offset)),
        ) = (&self.credit, &self.buffer) {
            credit.release(data.len() - offset);
        }
    }
}
